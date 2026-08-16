"""Live data pipeline: routes WebSocket ticks into the BarAggregator (spot) and
OptionChainSnapshot (options), and periodically refreshes per-strike Greeks. This is the
glue between broker/angelone_ws.py's raw tick callback and the strategy's market-data
inputs - nothing here makes trading decisions, it only keeps market data current.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from angel_auto.data.instruments import InstrumentMaster
from angel_auto.data.market_data import BarAggregator, OptionChainSnapshot, refresh_option_chain_greeks
from angel_auto.logging_conf import get_logger

log = get_logger(__name__)


class LiveFeedRouter:
    """The on_tick callback handed to AngelOneWebSocket - routes each tick by token."""

    def __init__(
        self,
        spot_token: str,
        bar_aggregator: BarAggregator,
        option_chain: OptionChainSnapshot,
        vix_token: str | None = None,
        tick_recorder=None,  # data.tick_recorder.TickRecorder | None - avoids a circular import
    ) -> None:
        self.spot_token = spot_token
        self.vix_token = vix_token
        self.bars = bar_aggregator
        self.option_chain = option_chain
        self.tick_recorder = tick_recorder
        self._latest_spot: float = 0.0
        self._latest_vix: float = 0.0
        self._spot_tick_listeners: list[Callable[[float], None]] = []

    def add_spot_tick_listener(self, listener: Callable[[float], None]) -> None:
        """Registered by the tick-driven zero-cross strategies (core/app.py) - called with
        the raw spot LTP on every spot tick, in addition to the candle-based BarAggregator
        the flagship strategy reads."""
        self._spot_tick_listeners.append(listener)

    def on_tick(self, tick: dict) -> None:
        token = str(tick.get("token", ""))
        raw_price = tick.get("last_traded_price")
        if not token or raw_price is None:
            return
        ltp = raw_price / 100.0  # Angel One ticks carry price in integer paise
        if ltp <= 0:
            return

        if token == self.spot_token:
            self._latest_spot = ltp
            self.bars.add_tick(ltp, datetime.now(timezone.utc))
            self._record_tick(token, "SPOT", None, ltp)
            for listener in self._spot_tick_listeners:
                try:
                    listener(ltp)
                except Exception:
                    log.exception("spot_tick_listener_failed")
        elif token == self.vix_token:
            self._latest_vix = ltp
            self._record_tick(token, "VIX", None, ltp)
        else:
            self.option_chain.update_ltp(token, ltp)
            quote = self.option_chain.get(token)
            self._record_tick(token, "OPTION", quote.trading_symbol if quote else None, ltp)

    def _record_tick(self, token: str, tick_type: str, trading_symbol: str | None, ltp: float) -> None:
        if self.tick_recorder is not None:
            self.tick_recorder.record(token, tick_type, trading_symbol, ltp)

    @property
    def latest_spot(self) -> float:
        return self._latest_spot

    @property
    def latest_vix(self) -> float:
        return self._latest_vix


def build_subscription_tokens(
    instruments: InstrumentMaster,
    option_chain: OptionChainSnapshot,
    underlying: str,
    expiry: str,
    center_strike: float,
    band_points: float,
    grid: float,
) -> list[str]:
    """Registers a quote slot (in `option_chain`) for every on-grid strike within
    `band_points` of `center_strike`, both CE and PE, and returns their tokens - ready to
    hand to AngelOneWebSocket.subscribe(). Called once per expiry roll (DEBIT->monthly,
    CREDIT->nearest - see strategy), not per tick.
    """
    tokens = []
    for inst in instruments.option_chain(underlying, expiry):
        if inst.strike % grid != 0:
            continue
        if abs(inst.strike - center_strike) > band_points:
            continue
        option_type = inst.symbol[-2:]
        option_chain.register(inst.token, inst.symbol, inst.strike, option_type, expiry=inst.expiry)
        tokens.append(inst.token)
    return tokens


class GreeksRefresher:
    """Periodically recomputes per-strike IV/delta on a background thread - a batch pass
    over every subscribed option, not a per-tick operation, since solving IV for each
    strike on every single tick would be wasteful."""

    def __init__(
        self,
        option_chain: OptionChainSnapshot,
        get_spot,  # Callable[[], float]
        get_expiry,  # Callable[[], str] - expiry can change if the strategy rolls DEBIT<->CREDIT
        rate: float,
        interval_sec: float = 5.0,
    ) -> None:
        self.option_chain = option_chain
        self.get_spot = get_spot
        self.get_expiry = get_expiry
        self.rate = rate
        self.interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="greeks-refresher", daemon=True)
        self._thread.start()
        log.info("greeks_refresher_started", interval_sec=self.interval_sec)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("greeks_refresher_stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._refresh_once()
            except Exception:
                log.exception("greeks_refresher_cycle_failed")
            self._stop_event.wait(self.interval_sec)

    def _refresh_once(self) -> None:
        spot = self.get_spot()
        if spot <= 0:
            return
        time_to_expiry_years = self._time_to_expiry_years(self.get_expiry())
        if time_to_expiry_years <= 0:
            return
        refresh_option_chain_greeks(self.option_chain, spot, time_to_expiry_years, self.rate)

    @staticmethod
    def _time_to_expiry_years(expiry: str) -> float:
        expiry_close = datetime.strptime(expiry, "%d%b%Y").replace(hour=15, minute=30)
        remaining_sec = (expiry_close - datetime.now()).total_seconds()
        return max(remaining_sec, 0.0) / (365 * 24 * 3600)

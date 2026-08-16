"""Composition root - wires the broker session, live data feed, strategy, OMS, and
scheduler into one running process. `mode` (config.yaml) decides which BrokerAdapter gets
bound; everything above that line (strategy, risk, OMS) is identical across modes.

mode: live requires more than editing config.yaml - see _require_live_trading_confirmation
below. That's deliberate: real orders should never activate from a one-line config edit.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import date

from angel_auto.broker.angelone_auth import AngelSession, login, logout, renew_session
from angel_auto.broker.angelone_rest import AngelOneBroker
from angel_auto.broker.angelone_ws import EXCHANGE_NSE_CM, EXCHANGE_NSE_FO, MODE_LTP, MODE_QUOTE, AngelOneWebSocket
from angel_auto.broker.base import BrokerAdapter
from angel_auto.broker.paper_broker import PaperBroker
from angel_auto.core.enums import Direction, Mode, StructureType
from angel_auto.data.historical import bootstrap_vix_history, capture_eod_vix_close
from angel_auto.data.instruments import InstrumentMaster
from angel_auto.data.live_feed import GreeksRefresher, LiveFeedRouter, build_subscription_tokens
from angel_auto.data.market_data import BarAggregator, OptionChainSnapshot
from angel_auto.data.tick_recorder import TickRecorder
from angel_auto.logging_conf import get_logger
from angel_auto.oms.order_manager import OrderManager
from angel_auto.persistence import journal
from angel_auto.persistence.db import init_db
from angel_auto.risk import circuit_breaker
from angel_auto.scheduler.jobs import SchedulerService, is_position_expiry_today
from angel_auto.settings import Settings, get_settings
from angel_auto.strategy.base import EntryIntent, ExitIntent
from angel_auto.strategy.macd_itm_otm_spread import MacdItmOtmSpreadStrategy
from angel_auto.strategy.macd_zero_cross_single_leg import MacdZeroCrossSingleLegStrategy

log = get_logger(__name__)

STRIKE_BAND_POINTS = 1000.0  # subscribe to on-grid strikes within this range of spot
SUBSCRIPTION_GRID_POINTS = 50.0  # real Nifty strike spacing - superset of the flagship's 100-pt grid,
# and what the zero-cross strategies' ATM/ITM4 selection needs live quotes for
FIRST_TICK_TIMEOUT_SEC = 10.0
VIX_BOOTSTRAP_LOOKBACK_DAYS = 90

# mode: live needs this env var set to exactly this value, in addition to config.yaml -
# an intentional second barrier so real-money trading can never turn on from a one-line
# config edit alone. Never put this in config.yaml/.env - set it by hand, in the shell,
# only when you mean it.
LIVE_TRADING_CONFIRM_ENV_VAR = "ANGEL_LIVE_TRADING_CONFIRMED"
LIVE_TRADING_CONFIRM_VALUE = "YES_I_UNDERSTAND_THE_RISK"


class TradingApp:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.app.mode == Mode.BACKTEST:
            raise NotImplementedError("backtest mode runs via scripts/run_backtest.py (BacktestEngine), not TradingApp")
        if self.settings.app.mode == Mode.LIVE:
            self._require_live_trading_confirmation()

        init_db()

        self.instruments = InstrumentMaster()
        self.bars = BarAggregator(interval_sec=self.settings.strategies.active.candle_interval_sec)
        self.option_chain = OptionChainSnapshot()

        self._session: AngelSession | None = None
        self._ws: AngelOneWebSocket | None = None
        self._router: LiveFeedRouter | None = None
        self._greeks_refresher: GreeksRefresher | None = None
        self._scheduler: SchedulerService | None = None
        self._market_data_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self.broker: BrokerAdapter | None = None
        self.strategy: MacdItmOtmSpreadStrategy | None = None
        self.zero_cross_strategies: dict[str, MacdZeroCrossSingleLegStrategy] = {}
        self.oms: OrderManager | None = None
        self.tick_recorder: TickRecorder | None = None

    @staticmethod
    def _require_live_trading_confirmation() -> None:
        if os.environ.get(LIVE_TRADING_CONFIRM_ENV_VAR) != LIVE_TRADING_CONFIRM_VALUE:
            raise RuntimeError(
                f"mode: live requires the environment variable {LIVE_TRADING_CONFIRM_ENV_VAR}="
                f"{LIVE_TRADING_CONFIRM_VALUE} to be set explicitly. This is deliberate - do not "
                "set it until paper trading has been validated (see the plan's pre-live checklist)."
            )
        log.warning("live_trading_confirmed_real_orders_will_be_placed")

    # --- Lifecycle -----------------------------------------------------

    def start(self) -> None:
        app_cfg = self.settings.app
        strat_cfg = self.settings.strategies.active
        log.info("app_starting", mode=app_cfg.mode.value)

        self._session = login(self.settings.credentials)
        self.instruments.load()
        spot = self.instruments.nifty_spot_instrument()
        vix = self.instruments.india_vix_instrument()

        try:
            bootstrap_vix_history(self._session, vix.token, VIX_BOOTSTRAP_LOOKBACK_DAYS)
        except Exception:
            log.exception("vix_history_bootstrap_failed_iv_rank_will_fall_back_to_debit")

        if app_cfg.mode == Mode.LIVE:
            self.broker = AngelOneBroker(self._session)
            log.warning("live_broker_adapter_active_real_orders_will_be_placed")
        else:
            self.broker = PaperBroker(self.option_chain, starting_capital_rs=app_cfg.paper_trading.starting_capital_rs)
        self.oms = OrderManager(
            self.broker,
            product_type=app_cfg.oms.product_type,
            entry_slippage_buffer_pts=app_cfg.oms.entry_slippage_buffer_pts,
            charges_config=app_cfg.charges,
        )

        if self.settings.strategies.flagship_enabled:
            self.strategy = MacdItmOtmSpreadStrategy(
                config=strat_cfg,
                underlying=app_cfg.underlying,
                lot_size=app_cfg.lot_size,
                max_trades_per_day=app_cfg.risk.max_trades_per_day,
                instruments=self.instruments,
                bar_aggregator=self.bars,
                option_chain=self.option_chain,
                get_current_vix=self._get_current_vix,
            )
        else:
            self.strategy = None

        for name in self.settings.strategies.zero_cross_strategies:
            cfg = self.settings.strategies.strategies[name]
            self.zero_cross_strategies[name] = MacdZeroCrossSingleLegStrategy(
                strategy_name=name,
                config=cfg,
                underlying=app_cfg.underlying,
                lot_size=app_cfg.lot_size,
                instruments=self.instruments,
                option_chain=self.option_chain,
            )

        if app_cfg.tick_recorder.enabled:
            self.tick_recorder = TickRecorder()
            self.tick_recorder.start()

        self._router = LiveFeedRouter(
            spot.token, self.bars, self.option_chain, vix_token=vix.token, tick_recorder=self.tick_recorder
        )
        self._router.add_spot_tick_listener(self._on_spot_tick)
        self._ws = AngelOneWebSocket(self._session, on_tick=self._router.on_tick)
        self._ws.start()
        self._ws.subscribe(EXCHANGE_NSE_CM, [spot.token, vix.token], mode=MODE_LTP)
        self._wait_for_first_spot_tick(FIRST_TICK_TIMEOUT_SEC)
        self._subscribe_option_band()

        self._greeks_refresher = GreeksRefresher(
            self.option_chain,
            get_spot=lambda: self._router.latest_spot,
            get_expiry=self._current_or_default_expiry,
            rate=app_cfg.risk_free_rate,
        )
        self._greeks_refresher.start()

        self._scheduler = SchedulerService(
            timezone=app_cfg.timezone,
            square_off_normal_time=app_cfg.square_off.normal_time,
            square_off_expiry_day_time=app_cfg.square_off.expiry_day_time,
            daily_relogin_time=app_cfg.scheduler.daily_relogin_time,
            on_square_off=self._handle_square_off,
            on_daily_relogin=self._handle_daily_relogin,
            is_expiry_day=self._any_open_position_expires_today,
            on_eod=self._handle_eod,
        )
        self._scheduler.start()

        self._market_data_thread = threading.Thread(target=self._market_data_loop, name="market-data-loop", daemon=True)
        self._market_data_thread.start()

        log.info("app_started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._market_data_thread:
            self._market_data_thread.join(timeout=5)
        if self._scheduler:
            self._scheduler.shutdown()
        if self._greeks_refresher:
            self._greeks_refresher.stop()
        if self.tick_recorder:
            self.tick_recorder.stop()
        if self._ws:
            self._ws.close()
        if self._session:
            logout(self._session)
        log.info("app_stopped")

    # --- Dashboard-facing controls (Phase 9 wires a UI onto these) ----------
    # These act on the flagship (manually-directed) strategy only - the zero-cross
    # strategies are fully automatic and have no Long/Short/Buying-Selling buttons.

    def request_direction(self, direction: Direction) -> EntryIntent | None:
        if self.strategy is None:
            return None
        intent = self.strategy.on_direction_request(direction)
        if intent is not None:
            self.oms.execute_entry(intent)
        return intent

    def request_structure(self, structure_type: StructureType) -> None:
        """Manual Buying/Selling button - sets the preference used at the next entry (see
        strategy.on_structure_request; a VIX spike can still override it at that moment)."""
        if self.strategy is not None:
            self.strategy.on_structure_request(structure_type)

    def cancel_pending(self) -> bool:
        return self.strategy.cancel_pending_request() if self.strategy is not None else False

    def manual_exit(self) -> None:
        if self.strategy is None:
            return
        intent = self.strategy.manual_exit()
        if intent is not None:
            self._dispatch_exit(intent)

    def kill_switch(self, reason: str = "manual kill-switch") -> None:
        circuit_breaker.trigger_kill_switch(reason)
        self.manual_exit()

    # --- Per-zero-cross-strategy controls (dashboard Exit Now / Kill Switch per panel) ---

    def manual_exit_for(self, strategy_name: str) -> None:
        strategy = self.zero_cross_strategies.get(strategy_name)
        if strategy is None:
            return
        intent = strategy.manual_exit()
        if intent is not None:
            self._dispatch_exit(intent, strategy_name=strategy_name)

    def kill_switch_for(self, strategy_name: str, reason: str = "manual kill-switch") -> None:
        circuit_breaker.trigger_kill_switch(reason, strategy_name=strategy_name)
        self.manual_exit_for(strategy_name)

    # --- Internal loops ---------------------------------------------------

    def _on_spot_tick(self, price: float) -> None:
        """Registered on LiveFeedRouter - fires once per incoming spot tick for each
        zero-cross strategy (tick-driven, unlike the flagship's 15-sec candle loop below)."""
        for name, strategy in self.zero_cross_strategies.items():
            try:
                intent = strategy.on_tick_price(price)
            except Exception:
                log.exception("zero_cross_tick_handling_failed", strategy=name)
                continue
            if isinstance(intent, EntryIntent):
                self.oms.execute_entry(intent, strategy_name=name)
            elif isinstance(intent, ExitIntent):
                self._dispatch_exit(intent, strategy_name=name)

    def _market_data_loop(self) -> None:
        interval = self.settings.strategies.active.candle_interval_sec
        while not self._stop_event.wait(interval):
            if self.strategy is None:
                continue
            try:
                intent = self.strategy.on_market_data()
            except Exception:
                log.exception("market_data_loop_cycle_failed")
                continue
            if isinstance(intent, EntryIntent):
                self.oms.execute_entry(intent)
            elif isinstance(intent, ExitIntent):
                self._dispatch_exit(intent)

    def _dispatch_exit(self, intent: ExitIntent, strategy_name: str = "macd_itm_otm_spread") -> None:
        risk_cfg = self.settings.app.risk
        if strategy_name in self.zero_cross_strategies:
            override = self.zero_cross_strategies[strategy_name].config.risk_override
            daily_loss_limit_rs = override.daily_loss_limit_rs
            max_consecutive_losses = override.max_consecutive_losses
        else:
            daily_loss_limit_rs = risk_cfg.daily_loss_limit_rs
            max_consecutive_losses = risk_cfg.max_consecutive_losses
        self.oms.execute_exit(
            intent,
            daily_loss_limit_rs=daily_loss_limit_rs,
            max_consecutive_losses=max_consecutive_losses,
            strategy_name=strategy_name,
        )

    def _handle_square_off(self) -> None:
        if self.strategy is not None:
            intent = self.strategy.on_square_off_trigger()
            if intent is not None:
                self._dispatch_exit(intent)
        for name, strategy in self.zero_cross_strategies.items():
            intent = strategy.on_square_off_trigger()
            if intent is not None:
                self._dispatch_exit(intent, strategy_name=name)

    def _any_open_position_expires_today(self) -> bool:
        """Whichever running strategy (flagship and/or the zero-cross strategies) has a
        position open today that also happens to expire today - determines whether the
        tighter 15:00 expiry-day square-off applies instead of the normal 15:15."""
        today = date.today()
        if is_position_expiry_today(journal.get_open_position(), today):
            return True
        return any(
            is_position_expiry_today(journal.get_open_position(strategy_name=name), today)
            for name in self.zero_cross_strategies
        )

    def _handle_daily_relogin(self) -> None:
        if self._session is not None:
            renew_session(self._session)

    def _handle_eod(self) -> None:
        capture_eod_vix_close(self._router.latest_vix)

    def _get_current_vix(self) -> float:
        vix = self._router.latest_vix
        if vix <= 0:
            raise RuntimeError("no live India VIX value yet")
        return vix

    def _current_or_default_expiry(self) -> str:
        open_position = journal.get_open_position()
        if open_position is not None:
            return open_position["expiry"]
        if self.strategy is None:
            return self.instruments.nearest_weekly_expiry(self.settings.app.underlying)
        return self.instruments.select_monthly_expiry(
            self.settings.app.underlying, self.settings.strategies.active.expiry.debit_min_days_gap
        )

    def _wait_for_first_spot_tick(self, timeout_sec: float) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline and self._router.latest_spot <= 0:
            time.sleep(0.2)
        if self._router.latest_spot <= 0:
            log.warning("no_spot_tick_received_before_subscribing_option_band_using_fallback")

    def _subscribe_option_band(self) -> None:
        center = self._router.latest_spot or 24000.0  # fallback if no spot tick arrived yet

        # The flagship (if enabled) trades whatever expiry its own structure rule picks
        # (monthly/nearest); the zero-cross strategies always trade the nearest weekly -
        # both bands need to be live-subscribed since either set of strategies can be running.
        expiries = set()
        if self.strategy is not None:
            expiries.add(self._current_or_default_expiry())
        if self.zero_cross_strategies:
            expiries.add(self.instruments.nearest_weekly_expiry(self.settings.app.underlying))
        if not expiries:
            expiries.add(self._current_or_default_expiry())

        tokens: list[str] = []
        for expiry in expiries:
            tokens.extend(
                build_subscription_tokens(
                    self.instruments,
                    self.option_chain,
                    self.settings.app.underlying,
                    expiry,
                    center_strike=center,
                    band_points=STRIKE_BAND_POINTS,
                    grid=SUBSCRIPTION_GRID_POINTS,
                )
            )
        if tokens:
            self._ws.subscribe(EXCHANGE_NSE_FO, tokens, mode=MODE_QUOTE)
        log.info("option_band_subscribed", expiries=sorted(expiries), center_strike=center, token_count=len(tokens))

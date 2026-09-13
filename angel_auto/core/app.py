"""Composition root - wires the broker session, live data feed, strategy, OMS, and
scheduler into one running process. `mode` (config.yaml) decides which BrokerAdapter gets
bound; everything above that line (strategy, risk, OMS) is identical across modes.

mode: live requires more than editing config.yaml - see _require_live_trading_confirmation
below. That's deliberate: real orders should never activate from a one-line config edit.

Resilience:
  - One trading lock serialises everything that can send orders - dashboard buttons (HTTP
    threads), the market-data loop and the scheduler's square-off - so two exits for the same
    position can never race each other.
  - No order goes out while the market is closed (the broker would queue it as an AMO for the
    next open), and no new entry while the price feed is stale.
  - A feed that goes quiet during market hours is reconnected (with a fresh login if needed)
    and every subscription re-sent - the SDK's own reconnect drops them.
  - On start, today's candles are rebuilt from the tick archive and the OMS settles whatever a
    crash left half-done (orders, OPENING/CLOSING positions, and - live - broker positions)
    before the loop may trade.
"""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from angel_auto.broker.angelone_auth import AngelSession, login, logout, renew_session
from angel_auto.broker.angelone_rest import AngelOneBroker
from angel_auto.broker.angelone_ws import EXCHANGE_NSE_CM, EXCHANGE_NSE_FO, MODE_LTP, MODE_QUOTE, AngelOneWebSocket
from angel_auto.broker.base import BrokerAdapter
from angel_auto.broker.paper_broker import PaperBroker
from angel_auto.core.enums import Direction, ExitReason, Mode, StructureType
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
from angel_auto.scheduler.jobs import (
    SchedulerService,
    is_market_open,
    is_position_expiry_today,
    is_trading_day,
    parse_hhmm,
)
from angel_auto.settings import Settings, get_settings
from angel_auto.strategy.base import EntryIntent, ExitIntent
from angel_auto.strategy.macd_itm_otm_spread import MacdItmOtmSpreadStrategy

log = get_logger(__name__)

FIRST_TICK_TIMEOUT_SEC = 10.0
FEED_STALE_SEC = 30.0  # no spot tick for this long during market hours = the feed is down
FEED_RESTART_COOLDOWN_SEC = 60.0

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

        self._trading_lock = threading.RLock()  # everything that can send an order holds this
        self._subscriptions: list[tuple[int, list[str], int]] = []  # re-sent after a feed reconnect
        self._last_feed_restart = 0.0

        self.broker: BrokerAdapter | None = None
        self.strategy: MacdItmOtmSpreadStrategy | None = None
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
            bootstrap_vix_history(self._session, vix.token, strat_cfg.iv_rank_lookback_days)
        except Exception:
            log.exception("vix_history_bootstrap_failed_iv_rank_will_fall_back_to_debit")

        live = app_cfg.mode == Mode.LIVE
        if live:
            self.broker = AngelOneBroker(self._session)
            log.warning("live_broker_adapter_active_real_orders_will_be_placed")
        else:
            self.broker = PaperBroker(
                self.option_chain,
                starting_capital_rs=app_cfg.paper_trading.starting_capital_rs,
                slippage_pct=app_cfg.paper_trading.slippage_pct,
            )
        oms_cfg = app_cfg.oms
        self.oms = OrderManager(
            self.broker,
            product_type=oms_cfg.product_type,
            entry_slippage_buffer_pts=oms_cfg.entry_slippage_buffer_pts,
            max_otm_retry_attempts=oms_cfg.entry_retry_attempts,
            retry_delay_sec=oms_cfg.entry_retry_delay_sec,
            charges_config=app_cfg.charges,
            fill_timeout_sec=oms_cfg.fill_timeout_sec,
            fill_poll_interval_sec=oms_cfg.fill_poll_interval_sec,
            exit_attempts=oms_cfg.exit_attempts,
            check_margin=live or app_cfg.paper_trading.simulate_margin_check,
            place_backup_sl=True,  # whether a given entry gets one is the strategy's call (EntryIntent.backup_sl_loss_rs)
            sl_limit_buffer_pts=oms_cfg.sl_limit_buffer_pts,
            order_gate=self._order_block_reason,
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

        self._restore_todays_candles()

        if app_cfg.tick_recorder.enabled:
            self.tick_recorder = TickRecorder()
            self.tick_recorder.start()

        self._router = LiveFeedRouter(
            spot.token, self.bars, self.option_chain, vix_token=vix.token, tick_recorder=self.tick_recorder
        )
        self._connect_feed()
        self._subscribe(EXCHANGE_NSE_CM, [spot.token, vix.token], MODE_LTP)
        self._wait_for_first_spot_tick(FIRST_TICK_TIMEOUT_SEC)
        self._subscribe_option_band()

        self._greeks_refresher = GreeksRefresher(
            self.option_chain,
            get_spot=lambda: self._router.latest_spot,
            rate=app_cfg.risk_free_rate,
            interval_sec=app_cfg.market_data.greeks_refresh_interval_sec,
        )
        self._greeks_refresher.start()

        self._recover_after_restart()

        self._scheduler = SchedulerService(
            timezone=app_cfg.timezone,
            square_off_normal_time=app_cfg.square_off.normal_time,
            square_off_expiry_day_time=app_cfg.square_off.expiry_day_time,
            daily_relogin_time=app_cfg.scheduler.daily_relogin_time,
            daily_reset_time=app_cfg.scheduler.daily_reset_time,
            on_square_off=self._handle_square_off,
            on_daily_relogin=self._handle_daily_relogin,
            is_expiry_day=lambda: is_position_expiry_today(journal.get_open_position(), date.today()),
            on_eod=self._handle_eod,
            eod_time=app_cfg.scheduler.eod_vix_capture_time,
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

    def request_direction(self, direction: Direction) -> EntryIntent | None:
        if self.strategy is None:
            return None
        with self._trading_lock:
            if self._square_off_due():
                self.strategy.record_entry_notice("Square-off ka time ho chuka hai - aaj naya trade nahi lagega.")
                return None
            intent = self.strategy.on_direction_request(direction)
            if intent is not None:
                self._execute_entry(intent)
            return intent

    def request_structure(self, structure_type: StructureType) -> None:
        """Manual Buying/Selling button - sets the preference used at the next entry (see
        strategy.on_structure_request; a VIX spike can still override it at that moment)."""
        if self.strategy is not None:
            self.strategy.on_structure_request(structure_type)

    def cancel_pending(self) -> bool:
        if self.strategy is None:
            return False
        with self._trading_lock:
            return self.strategy.cancel_pending_request()

    def manual_exit(self) -> None:
        if self.strategy is None:
            return
        with self._trading_lock:
            intent = self.strategy.manual_exit()
            if intent is not None:
                self._dispatch_exit(intent)

    def kill_switch(self, reason: str = "manual kill-switch") -> None:
        with self._trading_lock:
            circuit_breaker.trigger_kill_switch(reason)
            self.manual_exit()

    # --- Internal loops ---------------------------------------------------

    def _market_data_loop(self) -> None:
        interval = self.settings.strategies.active.check_interval_sec
        while not self._stop_event.wait(interval):
            try:
                self._check_feed()
            except Exception:
                log.exception("feed_check_failed")
            if self.strategy is None:
                continue
            try:
                self._run_cycle()
            except Exception:
                log.exception("market_data_loop_cycle_failed")

    def _run_cycle(self) -> None:
        with self._trading_lock:
            if self._square_off_due():
                # Retried every cycle until nothing is open - the scheduler's one-shot square-off
                # may have hit a rejected order - and no new entries from here on today.
                if journal.get_open_position() is not None:
                    self._handle_square_off()
                if journal.get_pending_direction_request() is not None:
                    self.strategy.cancel_pending_request()
                    self.strategy.record_entry_notice("Square-off ka time ho gaya - pending request cancel, aaj naya trade nahi.")
                return

            if self.oms.backup_sl_triggered():
                self._dispatch_exit(ExitIntent(reason=ExitReason.BROKER_SL))
                return

            intent = self.strategy.on_market_data()
            if isinstance(intent, EntryIntent):
                self._execute_entry(intent)
            elif isinstance(intent, ExitIntent):
                self._dispatch_exit(intent)

    def _square_off_due(self, now: datetime | None = None) -> bool:
        """At or after today's square-off time on a trading day - the tighter expiry-day time
        when the open position expires today."""
        now = now or datetime.now(ZoneInfo(self.settings.app.timezone))
        if not is_trading_day(now.date()):
            return False
        square_off = self.settings.app.square_off
        expiry_today = is_position_expiry_today(journal.get_open_position(), now.date())
        cutoff = parse_hhmm(square_off.expiry_day_time if expiry_today else square_off.normal_time)
        return now.time() >= cutoff

    def _market_open_now(self) -> bool:
        hours = self.settings.app.market_hours
        return is_market_open(datetime.now(ZoneInfo(self.settings.app.timezone)), parse_hhmm(hours.open), parse_hhmm(hours.close))

    def _order_block_reason(self, is_entry: bool) -> str | None:
        """The OMS's order gate. Outside market hours the broker would accept an order as an
        AMO and fire it at the next open; a new entry also needs a live feed (its strikes and
        the SL watching it run on those prices). Exits still go out on a stale feed - a MARKET
        exit doesn't need our price."""
        if not self._market_open_now():
            return "Market band hai - order nahi bheja (band market mein Angel One order ko AMO bana kar agle din chala deta hai)."
        if is_entry and self._router is not None and self._router.seconds_since_spot_tick() > FEED_STALE_SEC:
            return "Angel One se live price nahi aa rahe (feed ruka hai) - naya trade nahi bheja."
        return None

    def _execute_entry(self, intent: EntryIntent) -> None:
        position_id = self.oms.execute_entry(intent)
        if self.oms.last_notice:
            self.strategy.record_entry_notice(self.oms.last_notice)
        elif position_id is None:
            self.strategy.record_entry_notice("Order nahi laga - logs dekhein, phir dobara Long/Short dabayein.")

    def _dispatch_exit(self, intent: ExitIntent) -> None:
        with self._trading_lock:
            closed = self.oms.execute_exit(
                intent,
                daily_loss_limit_rs=self.settings.app.risk.daily_loss_limit_rs,
                max_consecutive_losses=self.settings.app.risk.max_consecutive_losses,
            )
        if not closed and self.oms.last_notice and self.strategy is not None:
            self.strategy.record_entry_notice(self.oms.last_notice)

    def _handle_square_off(self) -> None:
        if self.strategy is None:
            return
        with self._trading_lock:
            intent = self.strategy.on_square_off_trigger()
            if intent is not None:
                self._dispatch_exit(intent)

    def _handle_daily_relogin(self) -> None:
        """SEBI's 2026 API rules drop the long-lived refresh-token flow, so a failed renewal
        falls back to a full TOTP login and a fresh feed connection on it."""
        try:
            renew_session(self._session)
            return
        except Exception:
            log.warning("session_renew_failed_doing_fresh_login")
        try:
            self._relogin()
            self._restart_feed()
        except Exception:
            log.exception("daily_relogin_failed")
            if self.strategy is not None:
                self.strategy.record_entry_notice("Roz ka Angel One login fail hua - app band karke dobara chalu karein.")

    def _handle_eod(self) -> None:
        capture_eod_vix_close(self._router.latest_vix)

    def _get_current_vix(self) -> float:
        vix = self._router.latest_vix
        if vix <= 0:
            raise RuntimeError("no live India VIX value yet")
        return vix

    # --- Feed ---------------------------------------------------------------------

    def _connect_feed(self) -> None:
        self._ws = AngelOneWebSocket(self._session, on_tick=self._router.on_tick)
        self._ws.start()
        for exchange_type, tokens, mode in self._subscriptions:
            self._ws.subscribe(exchange_type, tokens, mode=mode)

    def _subscribe(self, exchange_type: int, tokens: list[str], mode: int) -> None:
        self._subscriptions.append((exchange_type, list(tokens), mode))
        self._ws.subscribe(exchange_type, tokens, mode=mode)

    def _check_feed(self) -> None:
        """Reconnects a feed that has gone quiet during market hours. The SDK's own reconnect
        loses every subscription and gives up after a few attempts, so it can't be relied on."""
        if self._router is None or not self._market_open_now():
            return
        age = self._router.seconds_since_spot_tick()
        if age < FEED_STALE_SEC:
            return
        now = time.monotonic()
        if now - self._last_feed_restart < FEED_RESTART_COOLDOWN_SEC:
            return
        self._last_feed_restart = now
        log.warning("feed_stale_reconnecting", seconds_since_spot_tick=None if math.isinf(age) else round(age))
        if self.strategy is not None:
            waited = "kaafi der" if math.isinf(age) else f"{age:.0f} sec"
            self.strategy.record_entry_notice(
                f"Angel One se live price {waited} se nahi aaye - feed dobara jod rahe hain. Tab tak naya trade nahi lagega."
            )
        self._restart_feed()

    def _restart_feed(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                log.exception("feed_close_failed")
        try:
            self._connect_feed()
        except Exception:
            log.exception("feed_reconnect_failed_retrying_with_fresh_login")
            self._relogin()
            self._connect_feed()
        log.info("feed_reconnected", subscriptions=len(self._subscriptions))

    def _relogin(self) -> None:
        with self._trading_lock:  # never swap the session under an order in flight
            self._session = login(self.settings.credentials)
            if isinstance(self.broker, AngelOneBroker):
                self.broker.session = self._session
        log.info("relogged_in")

    def _wait_for_first_spot_tick(self, timeout_sec: float) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline and self._router.latest_spot <= 0:
            time.sleep(0.2)
        if self._router.latest_spot <= 0:
            log.warning("no_spot_tick_received_before_subscribing_option_band_using_fallback")

    # --- Restart -------------------------------------------------------------------

    def _restore_todays_candles(self) -> None:
        """Rebuilds today's candles from the tick archive, so after a restart MACD (and the
        chart) carry on from where they were instead of starting cold."""
        if not self.settings.app.tick_recorder.enabled:
            return
        start_of_day = datetime.now(ZoneInfo(self.settings.app.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        ticks = journal.get_spot_ticks_since(start_of_day)
        for recorded_at, price in ticks:
            self.bars.add_tick(price, recorded_at)
        if ticks:
            log.info("candles_restored_from_tick_archive", ticks=len(ticks), candles=self.bars.candle_count)

    def _recover_after_restart(self) -> None:
        with self._trading_lock:
            notices = self.oms.recover_after_restart(
                daily_loss_limit_rs=self.settings.app.risk.daily_loss_limit_rs,
                max_consecutive_losses=self.settings.app.risk.max_consecutive_losses,
                verify_broker_positions=self.settings.app.mode == Mode.LIVE,
            )
        if notices and self.strategy is not None:
            self.strategy.record_entry_notice(" | ".join(notices))

    def _subscribe_option_band(self) -> None:
        strat_cfg = self.settings.strategies.active
        center = self._router.latest_spot or 24000.0  # fallback if no spot tick arrived yet
        position = journal.get_active_position()

        # Either structure can be chosen at the next entry (Buying/Selling button, or a VIX
        # override), so both configured expiries need live quotes up front - each on its own
        # button's strike grid.
        grid_by_expiry: dict[str, float] = {}
        if self.strategy is not None:
            for structure_type in (StructureType.DEBIT, StructureType.CREDIT):
                expiry = self.strategy.expiry_for(structure_type)
                grid = self.strategy.leg_rules(structure_type).strike_grid
                grid_by_expiry[expiry] = min(grid, grid_by_expiry.get(expiry, grid))

        tokens: list[str] = []
        for expiry, grid in grid_by_expiry.items():
            tokens.extend(
                build_subscription_tokens(
                    self.instruments,
                    self.option_chain,
                    self.settings.app.underlying,
                    expiry,
                    center_strike=center,
                    band_points=strat_cfg.option_band_points,
                    grid=grid,
                )
            )
        # An open position's own legs are always watched, wherever spot has moved since.
        if position is not None:
            for leg in position["legs"]:
                self.option_chain.register(
                    leg["token"], leg["trading_symbol"], leg["strike"], leg["option_type"].value, expiry=position["expiry"]
                )
                if leg["token"] not in tokens:
                    tokens.append(leg["token"])
        if tokens:
            self._subscribe(EXCHANGE_NSE_FO, tokens, MODE_QUOTE)
        log.info("option_band_subscribed", expiries=grid_by_expiry, center_strike=center, token_count=len(tokens))

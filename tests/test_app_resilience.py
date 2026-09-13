from datetime import date, datetime, timedelta, timezone

from angel_auto.core.app import FEED_STALE_SEC, TradingApp
from angel_auto.core.enums import Direction, ExitReason, LevelOrderStatus, StructureType
from angel_auto.persistence import journal
from angel_auto.strategy.base import EntryIntent, ExitIntent


class _Router:
    def __init__(self, seconds_since_tick):
        self.seconds = seconds_since_tick
        self.latest_spot = 24000.0
        self.latest_vix = 12.0

    def seconds_since_spot_tick(self):
        return self.seconds


def _app(seconds_since_tick, market_open=True) -> TradingApp:
    app = TradingApp()
    app._router = _Router(seconds_since_tick)
    app._market_open_now = lambda: market_open
    return app


def test_stale_feed_during_market_hours_reconnects_once_per_cooldown():
    app = _app(FEED_STALE_SEC + 5)
    restarts = []
    app._restart_feed = lambda: restarts.append(1)

    app._check_feed()
    app._check_feed()

    assert restarts == [1]


def test_fresh_feed_or_closed_market_does_not_reconnect():
    restarts = []
    fresh = _app(2.0)
    fresh._restart_feed = lambda: restarts.append(1)
    fresh._check_feed()
    closed = _app(999.0, market_open=False)
    closed._restart_feed = lambda: restarts.append(1)
    closed._check_feed()

    assert restarts == []


def test_no_orders_outside_market_hours_and_no_entries_on_a_stale_feed():
    closed = _app(1.0, market_open=False)
    assert "Market band" in closed._order_block_reason(is_entry=True)
    assert "Market band" in closed._order_block_reason(is_entry=False)

    assert _app(1.0)._order_block_reason(is_entry=True) is None

    stale = _app(FEED_STALE_SEC + 1)
    assert "feed" in stale._order_block_reason(is_entry=True)
    assert stale._order_block_reason(is_entry=False) is None  # a MARKET exit still goes out


def test_picking_an_expiry_subscribes_its_option_quotes_only_once(monkeypatch):
    import angel_auto.core.app as app_module

    built = []

    def fake_build(instruments, chain, underlying, expiry, **kwargs):
        built.append(expiry)
        return [f"{expiry}-token"]

    monkeypatch.setattr(app_module, "build_subscription_tokens", fake_build)

    class _Strategy:
        def on_expiry_request(self, expiry):
            return expiry != "01JAN2020"  # not on offer

    class _Ws:
        def __init__(self):
            self.calls = []

        def subscribe(self, exchange_type, tokens, mode):
            self.calls.append(list(tokens))

    app = _app(1.0)
    app.strategy = _Strategy()
    app._ws = _Ws()

    assert app.request_expiry("01JAN2020") is False
    assert app.request_expiry("22SEP2026") is True
    assert app.request_expiry("22SEP2026") is True  # picked again - already streaming

    assert built == ["22SEP2026"]
    assert app._ws.calls == [["22SEP2026-token"]]


def test_restart_rebuilds_todays_candles_from_the_tick_archive():
    now = datetime.now(timezone.utc)
    journal.bulk_insert_ticks([
        {"token": "26000", "tick_type": "SPOT", "trading_symbol": None, "ltp": 24000.0 + i,
         "recorded_at": now - timedelta(seconds=15 * (40 - i))}
        for i in range(40)
    ])
    journal.bulk_insert_ticks([{"token": "1", "tick_type": "OPTION", "trading_symbol": "X", "ltp": 99.0, "recorded_at": now}])
    app = TradingApp()

    app._restore_todays_candles()

    assert app.bars.candle_count >= 35
    assert app.bars.last_candle().close == 24039.0  # options ticks don't leak into spot candles


class _LevelStrategy:
    def __init__(self, intent):
        self.intent = intent
        self.notices = []

    def expire_stale_level_order(self):
        pass

    def on_spot_price(self, spot):
        return self.intent

    def record_entry_notice(self, message):
        self.notices.append(message)

    def cancel_pending_request(self):
        return True


class _EntryOms:
    def __init__(self, position_id, notice=None):
        self.position_id = position_id
        self.last_notice = notice

    def execute_entry(self, intent):
        return self.position_id


def _level_app(intent) -> TradingApp:
    app = _app(1.0)
    app._square_off_due = lambda now=None: False
    app.strategy = _LevelStrategy(intent)
    return app


def _waiting_level_order() -> int:
    return journal.create_level_order(date.today(), Direction.LONG, StructureType.DEBIT, "29SEP2026", 24850.0, "RISES_TO")


def test_level_entry_marks_the_level_order_executed():
    order_id = _waiting_level_order()
    app = _level_app(EntryIntent(Direction.LONG, StructureType.DEBIT, "29SEP2026", level_order_id=order_id))
    app.oms = _EntryOms(position_id=7)

    app._run_level_cycle()

    order = journal.get_level_order(order_id)
    assert (order["status"], order["position_id"]) == (LevelOrderStatus.EXECUTED, 7)


def test_failed_level_entry_marks_the_level_order_failed_with_the_reason():
    order_id = _waiting_level_order()
    app = _level_app(EntryIntent(Direction.LONG, StructureType.DEBIT, "29SEP2026", level_order_id=order_id))
    app.oms = _EntryOms(position_id=None, notice="Margin kam hai")

    app._run_level_cycle()

    order = journal.get_level_order(order_id)
    assert (order["status"], order["note"]) == (LevelOrderStatus.FAILED, "Margin kam hai")
    assert app.strategy.notices == ["Margin kam hai"]


def test_square_off_time_expires_a_waiting_level_order():
    order_id = _waiting_level_order()
    app = _level_app(intent=None)
    app._square_off_due = lambda now=None: True

    app._run_level_cycle()

    assert journal.get_level_order(order_id)["status"] == LevelOrderStatus.EXPIRED


def test_a_nifty_sl_exit_is_not_resent_every_second():
    app = _level_app(ExitIntent(reason=ExitReason.SPOT_SL))
    exits = []
    app._dispatch_exit = lambda intent: exits.append(intent.reason)

    app._run_level_cycle()
    app._run_level_cycle()  # one second later - the first attempt may still be failing at the broker

    assert exits == [ExitReason.SPOT_SL]

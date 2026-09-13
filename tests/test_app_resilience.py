from datetime import datetime, timedelta, timezone

from angel_auto.core.app import FEED_STALE_SEC, TradingApp
from angel_auto.persistence import journal


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

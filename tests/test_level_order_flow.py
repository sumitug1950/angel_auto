"""Nifty level orders end to end, the way they run in the live market: the app's 1-second loop,
the real strategy and OMS, and a broker that answers like Angel One - every order is
acknowledged OPEN and only fills on a later status poll."""
import pytest

import tests.test_order_fills as fills
from angel_auto.core.app import TradingApp
from angel_auto.core.enums import Direction, ExitReason, LevelOrderStatus, OrderSide, PositionStatus, StructureType
from angel_auto.persistence import journal
from tests.test_order_fills import OPEN, AngelLikeBroker, make_oms
from tests.test_strategy import (
    ALL_DELTAS,
    MONTHLY,
    STRIKES,
    UNDERLYING,
    _bearish_bars,
    _default_config,
    _fake_instruments,
    _make_strategy,
    _seed_option_chain,
)


class _Router:
    def __init__(self, spot):
        self.latest_spot = spot
        self.latest_vix = 15.0

    def seconds_since_spot_tick(self):
        return 1.0


@pytest.fixture
def live_like(monkeypatch):
    instruments = _fake_instruments([MONTHLY], STRIKES)
    chain = _seed_option_chain(instruments, MONTHLY, STRIKES, ALL_DELTAS)
    monkeypatch.setattr(fills, "LTP", {inst.token: 100.0 for inst in instruments.option_chain(UNDERLYING, MONTHLY)})
    app = TradingApp()
    # MACD is bearish throughout - a level order must not care
    app.strategy = _make_strategy(_default_config(), instruments, _bearish_bars(), chain, vix=15.0, expiry=MONTHLY)
    app.oms = make_oms(AngelLikeBroker())
    app._router = _Router(spot=24800.0)
    app._market_open_now = lambda: True
    app._square_off_due = lambda now=None: False
    return app, instruments


def _tick(app, spot):
    app._router.latest_spot = spot
    app._run_level_cycle()


def _token(instruments, strike, option_type="CE"):
    return instruments.find_option(UNDERLYING, MONTHLY, strike, option_type).token


def _sent(app, start=0):
    return [(r.token, r.side, getattr(r.order_type, "value", r.order_type)) for _, r in app.oms.broker.placed[start:]]


def test_level_entry_then_nifty_sl_exit_with_angel_one_style_fills(live_like):
    app, instruments = live_like
    order_id = app.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot_sl=24790.0, spot_target=24950.0)
    itm, otm = _token(instruments, 24600.0), _token(instruments, 25100.0)

    _tick(app, 24840.0)
    assert _sent(app) == []

    _tick(app, 24852.0)  # Nifty reaches the level
    assert _sent(app) == [(itm, OrderSide.BUY, "LIMIT"), (otm, OrderSide.SELL, "LIMIT")]
    position = journal.get_open_position()
    assert position["status"] == PositionStatus.OPEN
    assert (position["spot_sl"], position["spot_target"]) == (24790.0, 24950.0)
    order = journal.get_level_order(order_id)
    assert (order["status"], order["position_id"]) == (LevelOrderStatus.EXECUTED, position["id"])
    assert journal.get_or_create_daily_state()["trades_taken"] == 1

    _tick(app, 24800.0)  # between the Nifty SL and target
    assert len(_sent(app)) == 2

    _tick(app, 24789.0)  # Nifty SL
    assert _sent(app, start=2) == [(otm, OrderSide.BUY, "MARKET"), (itm, OrderSide.SELL, "MARKET")]  # short leg bought back first
    assert journal.get_open_position() is None
    assert journal.list_recent_positions(limit=1)[0]["exit_reason"] == ExitReason.SPOT_SL


def test_nifty_target_exit(live_like):
    app, _ = live_like
    app.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot_sl=24790.0, spot_target=24950.0)
    _tick(app, 24851.0)

    _tick(app, 24951.0)

    assert journal.get_open_position() is None
    assert journal.list_recent_positions(limit=1)[0]["exit_reason"] == ExitReason.SPOT_TARGET


def test_unfilled_level_entry_is_cancelled_at_the_broker_and_never_refires(live_like):
    app, instruments = live_like
    order_id = app.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0)
    app.oms.broker.script(_token(instruments, 24600.0), [OPEN])  # the LIMIT never fills

    _tick(app, 24855.0)

    assert app.oms.broker.cancelled  # not left resting at the broker
    assert journal.get_open_position() is None
    assert journal.get_level_order(order_id)["status"] == LevelOrderStatus.FAILED
    _tick(app, 24860.0)
    assert len(_sent(app)) == 1


def test_waiting_level_order_carries_on_after_a_restart(live_like):
    app, instruments = live_like
    app.place_level_order(Direction.SHORT, StructureType.CREDIT, 24700.0, spot_sl=24760.0)
    # restart: a brand-new strategy object that only knows what is in the database
    app.strategy = _make_strategy(
        _default_config(), instruments, _bearish_bars(), app.strategy.option_chain, vix=15.0, expiry=MONTHLY
    )

    _tick(app, 24699.0)

    otm, itm = _token(instruments, 25100.0), _token(instruments, 24600.0)
    assert _sent(app) == [(otm, OrderSide.BUY, "LIMIT"), (itm, OrderSide.SELL, "LIMIT")]  # Selling: hedge bought first
    assert journal.get_open_position()["spot_sl"] == 24760.0


def test_kill_switch_cancels_a_waiting_level_order(live_like):
    app, _ = live_like
    order_id = app.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0)

    app.kill_switch("test")
    _tick(app, 24900.0)

    assert journal.get_level_order(order_id)["status"] == LevelOrderStatus.CANCELLED
    assert _sent(app) == []

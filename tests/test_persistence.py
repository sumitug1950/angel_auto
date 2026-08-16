from datetime import date

from angel_auto.core.enums import (
    Direction,
    ExitReason,
    OptionType,
    OrderSide,
    OrderStatus,
    PendingRequestStatus,
    PositionStatus,
    StructureType,
)
from angel_auto.persistence import journal


def test_vix_history_upsert_and_read():
    journal.upsert_vix_close(date(2026, 8, 10), 13.5)
    journal.upsert_vix_close(date(2026, 8, 11), 14.2)
    journal.upsert_vix_close(date(2026, 8, 11), 14.5)  # overwrite same day

    history = journal.get_vix_history(lookback_days=90, as_of=date(2026, 8, 11))
    assert sorted(history) == [13.5, 14.5]


def test_get_previous_vix_close_returns_most_recent_prior_day():
    journal.upsert_vix_close(date(2026, 8, 10), 13.5)
    journal.upsert_vix_close(date(2026, 8, 11), 14.2)

    assert journal.get_previous_vix_close(before_date=date(2026, 8, 12)) == 14.2


def test_get_previous_vix_close_ignores_same_day_entry():
    # today's own close (if already recorded) must not be returned as "previous"
    journal.upsert_vix_close(date(2026, 8, 10), 13.5)
    journal.upsert_vix_close(date(2026, 8, 11), 14.2)

    assert journal.get_previous_vix_close(before_date=date(2026, 8, 11)) == 13.5


def test_get_previous_vix_close_none_when_no_history():
    assert journal.get_previous_vix_close(before_date=date(2026, 8, 11)) is None


def test_structure_preference_defaults_when_never_set():
    assert journal.get_structure_preference() == StructureType.DEBIT


def test_structure_preference_set_and_get():
    journal.set_structure_preference(StructureType.CREDIT)
    assert journal.get_structure_preference() == StructureType.CREDIT


def test_structure_preference_overwrites_previous_value():
    journal.set_structure_preference(StructureType.CREDIT)
    journal.set_structure_preference(StructureType.DEBIT)
    assert journal.get_structure_preference() == StructureType.DEBIT


def test_daily_state_create_and_increment():
    state = journal.get_or_create_daily_state(date(2026, 8, 16))
    assert state["trades_taken"] == 0
    assert state["trading_halted"] is False

    journal.increment_daily_trade_count(date(2026, 8, 16))
    count = journal.increment_daily_trade_count(date(2026, 8, 16))
    assert count == 2


def test_record_trade_pnl_tracks_consecutive_losses():
    d = date(2026, 8, 16)
    state = journal.record_trade_pnl(-4000, trade_date=d)
    assert state["consecutive_losses"] == 1
    assert state["realized_pnl_rs"] == -4000

    state = journal.record_trade_pnl(-4000, trade_date=d)
    assert state["consecutive_losses"] == 2
    assert state["realized_pnl_rs"] == -8000

    state = journal.record_trade_pnl(6000, trade_date=d)
    assert state["consecutive_losses"] == 0  # win resets the streak
    assert state["realized_pnl_rs"] == -2000


def test_trading_halt():
    journal.set_trading_halt("daily loss limit hit", date(2026, 8, 16))
    state = journal.get_or_create_daily_state(date(2026, 8, 16))
    assert state["trading_halted"] is True
    assert "daily loss limit" in state["halt_reason"]


def test_direction_request_lifecycle():
    request_id = journal.create_direction_request(Direction.LONG, macd_state_at_request="BEARISH")
    pending = journal.get_pending_direction_request()
    assert pending is not None
    assert pending["id"] == request_id
    assert pending["direction"] == Direction.LONG

    journal.resolve_direction_request(request_id, PendingRequestStatus.EXECUTED)
    assert journal.get_pending_direction_request() is None


def test_full_position_lifecycle():
    assert journal.get_open_position() is None

    request_id = journal.create_direction_request(Direction.LONG, macd_state_at_request="BULLISH")
    position_id = journal.create_position(
        Direction.LONG, StructureType.DEBIT, expiry="18AUG2026", direction_request_id=request_id, iv_rank_at_entry=35.0
    )

    itm_leg_id = journal.add_leg(
        position_id, token="45116", trading_symbol="NIFTY18AUG2624600CE", option_type=OptionType.CE,
        strike=24600.0, role="ITM", side=OrderSide.BUY, quantity=65, delta_at_selection=0.7,
    )
    otm_leg_id = journal.add_leg(
        position_id, token="45200", trading_symbol="NIFTY18AUG2625200CE", option_type=OptionType.CE,
        strike=25200.0, role="OTM", side=OrderSide.SELL, quantity=65, delta_at_selection=0.1,
    )

    order_id = journal.add_order(itm_leg_id, side=OrderSide.BUY, order_type="LIMIT", quantity=65, price=290.0)
    journal.update_order_status(order_id, OrderStatus.FILLED, broker_order_id="BROKER123", filled_price=291.5)
    journal.update_leg_fill(itm_leg_id, entry_price=291.5)

    journal.update_position_status(position_id, PositionStatus.OPEN, set_entry_time=True)

    open_position = journal.get_open_position()
    assert open_position is not None
    assert open_position["id"] == position_id
    assert len(open_position["legs"]) == 2
    itm = next(leg for leg in open_position["legs"] if leg["role"] == "ITM")
    assert itm["entry_price"] == 291.5

    journal.update_trailing_peak(position_id, peak_profit_rs=6000.0, trail_active=True)

    journal.close_position(position_id, ExitReason.TRAILING_STOP, realized_pnl_rs=5500.0)

    assert journal.get_open_position() is None
    daily_state = journal.get_or_create_daily_state()
    assert daily_state["realized_pnl_rs"] == 5500.0
    assert daily_state["consecutive_losses"] == 0


def test_equity_curve_append_does_not_raise():
    journal.append_equity_point(realized_pnl_rs=1000.0, unrealized_pnl_rs=-200.0, total_equity_rs=20800.0)

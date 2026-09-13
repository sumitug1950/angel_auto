"""OMS against a broker that behaves like Angel One: every order is acknowledged OPEN and only
reaches a final state later, via get_order_state - fills are confirmed, never assumed."""
import pytest

from angel_auto.broker.base import (
    BrokerAdapter,
    MarginCheckResult,
    MarginLeg,
    OrderRequest,
    OrderResult,
    OrderState,
    PositionSnapshot,
)
from angel_auto.broker.paper_broker import PaperBroker
from angel_auto.core.enums import Direction, ExitReason, OptionType, OrderSide, OrderStatus, PositionStatus, StructureType
from angel_auto.data.market_data import OptionChainSnapshot
from angel_auto.oms.order_manager import OrderManager
from angel_auto.persistence import journal
from angel_auto.persistence.db import session_scope
from angel_auto.persistence.models import Order, Position
from angel_auto.strategy.base import EntryIntent, ExitIntent, LegIntent

LOT = 65
LTP = {"ITM": 300.0, "OTM": 65.0}
OPEN = OrderState(OrderStatus.OPEN)


def filled(price: float, qty: int = LOT) -> OrderState:
    return OrderState(OrderStatus.FILLED, qty, price)


class AngelLikeBroker(BrokerAdapter):
    """Orders come back OPEN; each token's upcoming orders follow a queued script of states
    (the last state repeats), with an optional state to switch to when cancelled. Unscripted
    orders fill at the token's LTP on the first poll."""

    def __init__(self):
        self.placed: list[tuple[str, OrderRequest]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.margin = MarginCheckResult(1000.0, 1_000_000.0, True)
        self.positions: list[PositionSnapshot] = []
        self._scripts: dict[str, list] = {}
        self._orders: dict[str, dict] = {}

    def script(self, token, states, on_cancel=None):
        self._scripts.setdefault(token, []).append((list(states), on_cancel))

    def add_existing(self, order_id, states, on_cancel=None, tag=""):
        """An order the broker already has - e.g. one sent before the app crashed."""
        self._orders[order_id] = {"states": list(states), "on_cancel": on_cancel, "tag": tag}

    def place_order(self, request):
        order_id = f"O{len(self.placed) + 1}"
        self.placed.append((order_id, request))
        self.calls.append(("place", order_id))
        queue = self._scripts.get(request.token) or []
        states, on_cancel = queue.pop(0) if queue else ([filled(LTP[request.token], request.quantity)], None)
        self._orders[order_id] = {"states": states, "on_cancel": on_cancel, "tag": request.tag}
        return OrderResult(order_id, OrderStatus.OPEN)

    def find_order_by_tag(self, tag):
        for order_id, order in reversed(list(self._orders.items())):
            if order["tag"] == tag:
                return order_id, self.get_order_state(order_id)
        return None

    def get_order_state(self, broker_order_id):
        states = self._orders[broker_order_id]["states"]
        return states.pop(0) if len(states) > 1 else states[0]

    def cancel_order(self, broker_order_id, variety="NORMAL"):
        self.cancelled.append((broker_order_id, variety))
        self.calls.append(("cancel", broker_order_id))
        order = self._orders[broker_order_id]
        order["states"] = [order["on_cancel"] or OrderState(OrderStatus.CANCELLED)]

    def get_ltp(self, exchange, trading_symbol, token):
        return LTP[token]

    def check_margin(self, legs):
        return self.margin

    def get_positions(self):
        return self.positions


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def make_oms(broker, **kwargs) -> OrderManager:
    clock = FakeClock()
    return OrderManager(
        broker, retry_delay_sec=0, fill_timeout_sec=5, fill_poll_interval_sec=1, sleep=clock.sleep, clock=clock.now, **kwargs
    )


def _leg(role: str, side: OrderSide) -> LegIntent:
    return LegIntent(
        option_type=OptionType.CE, strike=24600.0 if role == "ITM" else 25100.0, role=role, side=side,
        token=role, trading_symbol=f"NIFTY{role}", quantity=LOT, delta_at_selection=0.5,
    )


def debit(backup_sl_loss_rs=None) -> EntryIntent:
    return EntryIntent(
        direction=Direction.LONG, structure_type=StructureType.DEBIT, expiry="29SEP2026",
        legs=[_leg("ITM", OrderSide.BUY), _leg("OTM", OrderSide.SELL)], backup_sl_loss_rs=backup_sl_loss_rs,
    )


def credit(backup_sl_loss_rs=None) -> EntryIntent:
    return EntryIntent(
        direction=Direction.SHORT, structure_type=StructureType.CREDIT, expiry="20AUG2026",
        legs=[_leg("ITM", OrderSide.SELL), _leg("OTM", OrderSide.BUY)], backup_sl_loss_rs=backup_sl_loss_rs,
    )


def manual_exit(oms, reason=ExitReason.MANUAL_EXIT) -> bool:
    return oms.execute_exit(ExitIntent(reason=reason), daily_loss_limit_rs=100_000, max_consecutive_losses=5)


def leg_of(role: str) -> dict:
    return next(leg for leg in journal.get_open_position()["legs"] if leg["role"] == role)


# --- Entry fills ----------------------------------------------------------------------


def test_entry_waits_for_a_late_fill_instead_of_cancelling():
    broker = AngelLikeBroker()
    broker.script("ITM", [OPEN, OPEN, filled(301.0)])
    oms = make_oms(broker)

    assert oms.execute_entry(debit()) is not None
    assert leg_of("ITM")["entry_price"] == 301.0
    assert broker.cancelled == []


def test_order_that_fills_while_being_cancelled_counts_as_filled():
    broker = AngelLikeBroker()
    broker.script("ITM", [OPEN], on_cancel=filled(300.5))  # still working at the timeout
    oms = make_oms(broker)

    assert oms.execute_entry(debit()) is not None
    assert leg_of("ITM")["entry_price"] == 300.5
    assert len(broker.cancelled) == 1


def test_rejected_entry_aborts_without_sending_the_second_leg_and_says_why():
    broker = AngelLikeBroker()
    broker.script("ITM", [OrderState(OrderStatus.REJECTED, message="RMS:Margin Exceeds")])
    oms = make_oms(broker)

    assert oms.execute_entry(debit()) is None
    assert journal.get_open_position() is None
    assert len(broker.placed) == 1
    assert "Margin Exceeds" in oms.last_notice


def test_partially_filled_entry_leg_is_closed_again():
    broker = AngelLikeBroker()
    broker.script("ITM", [OPEN], on_cancel=OrderState(OrderStatus.CANCELLED, 30, 300.0))
    oms = make_oms(broker)

    assert oms.execute_entry(debit()) is None
    unwind = broker.placed[-1][1]
    assert (unwind.token, unwind.side, unwind.quantity, unwind.order_type) == ("ITM", OrderSide.SELL, 30, "MARKET")
    assert oms.frozen_reason is None


def test_insufficient_margin_blocks_entry_before_any_order():
    broker = AngelLikeBroker()
    broker.margin = MarginCheckResult(55364.2, 20000.0, False)
    oms = make_oms(broker, check_margin=True)

    assert oms.execute_entry(credit()) is None
    assert broker.placed == []
    assert "55,364" in oms.last_notice and "20,000" in oms.last_notice
    with session_scope() as session:
        assert session.query(Position).count() == 0


# --- Exit fills -----------------------------------------------------------------------


def test_exit_that_confirms_late_is_sent_exactly_once_per_leg():
    broker = AngelLikeBroker()
    oms = make_oms(broker)
    oms.execute_entry(debit())
    broker.script("OTM", [OPEN, OPEN, filled(60.0)])
    placed_before = len(broker.placed)

    assert manual_exit(oms) is True
    assert len(broker.placed) - placed_before == 2
    assert journal.get_open_position() is None
    assert journal.get_or_create_daily_state()["realized_pnl_rs"] == pytest.approx((65.0 - 60.0) * LOT)


def test_exit_order_with_unknown_fate_freezes_instead_of_resending():
    broker = AngelLikeBroker()
    oms = make_oms(broker)
    oms.execute_entry(debit())
    broker.script("OTM", [OPEN], on_cancel=OPEN)  # never confirms, even after cancel

    assert manual_exit(oms) is False
    assert oms.frozen_reason and "Angel One" in oms.frozen_reason
    assert journal.get_open_position() is not None  # still visible on the dashboard
    assert journal.get_or_create_daily_state()["trading_halted"] is True

    placed = len(broker.placed)
    assert manual_exit(oms) is False
    assert oms.execute_entry(debit()) is None
    assert len(broker.placed) == placed  # nothing new sent while frozen


def test_failed_long_leg_exit_keeps_position_and_retry_closes_only_that_leg():
    broker = AngelLikeBroker()
    oms = make_oms(broker)
    oms.execute_entry(debit())
    broker.script("ITM", [OrderState(OrderStatus.REJECTED, message="exchange busy")])

    assert manual_exit(oms) is False  # short OTM bought back, long ITM sale rejected
    assert leg_of("OTM")["exit_price"] == 65.0
    assert "exchange busy" in oms.last_notice

    placed = len(broker.placed)
    assert manual_exit(oms) is True
    assert [request.token for _, request in broker.placed[placed:]] == ["ITM"]


# --- Broker backup SL -----------------------------------------------------------------


def _credit_with_resting_backup_sl(broker, **kwargs) -> OrderManager:
    broker.script("ITM", [filled(110.0)])  # the ITM short
    broker.script("ITM", [OPEN])  # its backup SL, resting at the broker
    oms = make_oms(broker, place_backup_sl=True, sl_limit_buffer_pts=5.0, **kwargs)
    assert oms.execute_entry(credit(backup_sl_loss_rs=6000.0)) is not None
    return oms


def test_credit_entry_places_broker_backup_sl_on_the_short_leg():
    broker = AngelLikeBroker()
    _credit_with_resting_backup_sl(broker)

    _, sl = broker.placed[-1]
    assert (sl.token, sl.side, sl.order_type, sl.variety) == ("ITM", OrderSide.BUY, "SL", "STOPLOSS")
    assert sl.trigger_price == 202.35  # 110 + 6000/65 = 202.31 -> next 0.05 tick
    assert sl.price == 207.35


def test_debit_entry_gets_no_broker_backup_sl():
    broker = AngelLikeBroker()
    oms = make_oms(broker, place_backup_sl=True)
    assert oms.execute_entry(debit(backup_sl_loss_rs=6000.0)) is not None
    assert all(request.order_type == "LIMIT" for _, request in broker.placed)


def test_exit_cancels_backup_sl_before_closing_any_leg():
    broker = AngelLikeBroker()
    oms = _credit_with_resting_backup_sl(broker)
    sl_order_id = broker.placed[-1][0]
    calls_before = len(broker.calls)

    assert manual_exit(oms) is True
    later = broker.calls[calls_before:]
    assert later[0] == ("cancel", sl_order_id)
    assert all(kind == "place" for kind, _ in later[1:])
    assert (sl_order_id, "STOPLOSS") in broker.cancelled


def test_triggered_backup_sl_is_detected_and_its_fill_closes_the_short_leg():
    broker = AngelLikeBroker()
    broker.script("ITM", [filled(110.0)])
    broker.script("ITM", [OPEN, filled(205.0)])  # resting, then triggered at the broker
    oms = make_oms(broker, place_backup_sl=True)
    oms.execute_entry(credit(backup_sl_loss_rs=6000.0))

    assert oms.backup_sl_triggered() is False
    assert oms.backup_sl_triggered() is True

    placed = len(broker.placed)
    assert manual_exit(oms, ExitReason.BROKER_SL) is True
    assert [request.token for _, request in broker.placed[placed:]] == ["OTM"]  # short already closed by the SL
    assert journal.get_or_create_daily_state()["realized_pnl_rs"] == pytest.approx((110.0 - 205.0) * LOT)


# --- Paper broker simulation ------------------------------------------------------------


def _chain(**quotes) -> OptionChainSnapshot:
    chain = OptionChainSnapshot()
    for token, (strike, ltp) in quotes.items():
        chain.register(token, f"SYM{token}", strike, "CE")
        chain.update_ltp(token, ltp)
    return chain


def test_paper_broker_simulates_a_stop_loss_trigger():
    chain = _chain(T=(24600.0, 110.0))
    broker = PaperBroker(chain, starting_capital_rs=100_000, slippage_pct=0.0)
    result = broker.place_order(OrderRequest(
        exchange="NFO", trading_symbol="SYMT", token="T", side=OrderSide.BUY, quantity=LOT, order_type="SL",
        product_type="INTRADAY", price=207.35, trigger_price=202.35, variety="STOPLOSS",
    ))
    assert result.status == OrderStatus.OPEN
    assert broker.get_order_state(result.broker_order_id).status == OrderStatus.OPEN

    chain.update_ltp("T", 203.0)
    state = broker.get_order_state(result.broker_order_id)
    assert (state.status, state.filled_quantity, state.average_price) == (OrderStatus.FILLED, LOT, 203.0)


def test_paper_margin_prices_spreads_by_their_risk():
    chain = _chain(ITM=(24600.0, 300.0), OTM=(24800.0, 150.0))
    broker = PaperBroker(chain, starting_capital_rs=100_000)

    def margin(itm_side, otm_side):
        return broker.check_margin([
            MarginLeg("NFO", "SYMITM", "ITM", itm_side, LOT, "INTRADAY"),
            MarginLeg("NFO", "SYMOTM", "OTM", otm_side, LOT, "INTRADAY"),
        ]).required_margin_rs

    assert margin(OrderSide.BUY, OrderSide.SELL) == pytest.approx((300 - 150) * LOT)  # debit: the net premium
    assert margin(OrderSide.SELL, OrderSide.BUY) == pytest.approx(200 * LOT - (300 - 150) * LOT)  # credit: width - credit


# --- Order gate & tags -------------------------------------------------------------------


def test_order_gate_blocks_entries_and_exits_with_its_reason():
    broker = AngelLikeBroker()
    make_oms(broker).execute_entry(debit())
    placed = len(broker.placed)
    gated = make_oms(broker, order_gate=lambda is_entry: "Market band hai")

    assert gated.execute_entry(debit()) is None
    assert manual_exit(gated) is False
    assert gated.last_notice == "Market band hai"
    assert len(broker.placed) == placed
    assert journal.get_open_position() is not None


def test_every_order_carries_its_journal_id_as_its_broker_tag():
    broker = AngelLikeBroker()
    make_oms(broker).execute_entry(debit())
    with session_scope() as session:
        order_ids = {order.id for order in session.query(Order)}
    assert {request.tag for _, request in broker.placed} == {f"AA{order_id}" for order_id in order_ids}


# --- Restart recovery -------------------------------------------------------------------


def _position_with_legs(status: PositionStatus, legs) -> dict[str, int]:
    """legs: (role, side, entry_price, exit_price). Returns {role: leg_id}."""
    position_id = journal.create_position(Direction.LONG, StructureType.DEBIT, "29SEP2026")
    leg_ids = {}
    for role, side, entry, exit_ in legs:
        leg_id = journal.add_leg(position_id, role, f"NIFTY{role}", OptionType.CE, 24600.0, role, side, LOT)
        if entry is not None:
            journal.update_leg_fill(leg_id, entry_price=entry)
        if exit_ is not None:
            journal.update_leg_fill(leg_id, exit_price=exit_)
        leg_ids[role] = leg_id
    journal.update_position_status(position_id, status, set_entry_time=status != PositionStatus.OPENING)
    return leg_ids


def _order_left_by_crash(broker, leg_id, side, order_type, states, on_cancel=None, id_was_saved=True) -> int:
    order_id = journal.add_order(leg_id, side, order_type, LOT)
    broker.add_existing(f"B{order_id}", states, on_cancel, tag=f"AA{order_id}")
    if id_was_saved:
        journal.update_order_status(order_id, OrderStatus.OPEN, broker_order_id=f"B{order_id}")
    return order_id


def recover(oms, verify=False):
    return oms.recover_after_restart(daily_loss_limit_rs=100_000, max_consecutive_losses=5, verify_broker_positions=verify)


def _only_position_exit_reason():
    with session_scope() as session:
        return session.query(Position).one().exit_reason


def test_recovery_settles_an_exit_that_filled_while_the_app_was_down():
    broker = AngelLikeBroker()
    legs = _position_with_legs(PositionStatus.CLOSING, [("ITM", OrderSide.BUY, 300.0, None), ("OTM", OrderSide.SELL, 65.0, None)])
    _order_left_by_crash(broker, legs["OTM"], OrderSide.BUY, "MARKET", [filled(60.0)])

    notices = recover(make_oms(broker))

    assert journal.get_open_position() is not None  # the long leg still has to be closed
    assert leg_of("OTM")["exit_price"] == 60.0
    assert leg_of("ITM")["exit_price"] is None
    assert any("adhura" in notice for notice in notices)
    assert broker.placed == []  # recovery never sends a new order itself


def test_recovery_books_a_position_whose_exit_had_fully_completed():
    broker = AngelLikeBroker()
    legs = _position_with_legs(PositionStatus.CLOSING, [("ITM", OrderSide.BUY, 300.0, None), ("OTM", OrderSide.SELL, 65.0, 60.0)])
    _order_left_by_crash(broker, legs["ITM"], OrderSide.SELL, "MARKET", [filled(310.0)])

    recover(make_oms(broker))

    assert journal.get_open_position() is None
    assert journal.get_or_create_daily_state()["realized_pnl_rs"] == pytest.approx((310 - 300) * LOT + (65 - 60) * LOT)
    assert _only_position_exit_reason() == ExitReason.RECOVERED


def test_recovery_opens_a_position_whose_entry_filled_during_the_crash():
    broker = AngelLikeBroker()
    legs = _position_with_legs(PositionStatus.OPENING, [("ITM", OrderSide.BUY, None, None)])
    _order_left_by_crash(broker, legs["ITM"], OrderSide.BUY, "LIMIT", [filled(301.0)])  # already filled by restart time

    recover(make_oms(broker))

    assert broker.cancelled == []
    assert leg_of("ITM")["entry_price"] == 301.0
    assert journal.get_open_position()["status"] == PositionStatus.OPEN
    assert journal.get_or_create_daily_state()["trades_taken"] == 1


def test_recovery_cancels_a_still_working_entry_and_aborts_when_nothing_filled():
    broker = AngelLikeBroker()
    legs = _position_with_legs(PositionStatus.OPENING, [("ITM", OrderSide.BUY, None, None)])
    order_id = _order_left_by_crash(broker, legs["ITM"], OrderSide.BUY, "LIMIT", [OPEN], on_cancel=OrderState(OrderStatus.CANCELLED))

    recover(make_oms(broker))

    assert broker.cancelled == [(f"B{order_id}", "NORMAL")]
    assert journal.get_open_position() is None


def test_recovery_finds_an_order_sent_before_its_id_was_saved_by_its_tag():
    broker = AngelLikeBroker()
    legs = _position_with_legs(PositionStatus.OPENING, [("ITM", OrderSide.BUY, None, None)])
    _order_left_by_crash(broker, legs["ITM"], OrderSide.BUY, "LIMIT", [filled(299.0)], id_was_saved=False)

    recover(make_oms(broker))

    assert leg_of("ITM")["entry_price"] == 299.0


def test_recovery_books_a_position_already_flat_at_the_broker():
    broker = AngelLikeBroker()
    _position_with_legs(PositionStatus.OPEN, [("ITM", OrderSide.BUY, 300.0, None), ("OTM", OrderSide.SELL, 65.0, None)])

    notices = recover(make_oms(broker), verify=True)

    assert journal.get_open_position() is None
    assert _only_position_exit_reason() == ExitReason.EXTERNAL
    assert any("pehle hi band" in notice for notice in notices)


def test_recovery_freezes_when_broker_positions_disagree():
    broker = AngelLikeBroker()
    _position_with_legs(PositionStatus.OPEN, [("ITM", OrderSide.BUY, 300.0, None)])
    broker.positions = [PositionSnapshot("NFO", "NIFTYITM", "ITM", OrderSide.BUY, 2 * LOT, 300.0, 300.0)]
    oms = make_oms(broker)

    recover(oms, verify=True)

    assert oms.frozen_reason and "nahi milti" in oms.frozen_reason
    assert journal.get_open_position() is not None


def test_recovery_leaves_matching_broker_positions_alone():
    broker = AngelLikeBroker()
    _position_with_legs(PositionStatus.OPEN, [("ITM", OrderSide.BUY, 300.0, None), ("OTM", OrderSide.SELL, 65.0, None)])
    broker.positions = [
        PositionSnapshot("NFO", "NIFTYITM", "ITM", OrderSide.BUY, LOT, 300.0, 300.0),
        PositionSnapshot("NFO", "NIFTYOTM", "OTM", OrderSide.SELL, LOT, 65.0, 65.0),
    ]
    oms = make_oms(broker)

    assert recover(oms, verify=True) == []
    assert oms.frozen_reason is None
    assert journal.get_open_position() is not None

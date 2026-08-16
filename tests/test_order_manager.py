from angel_auto.broker.paper_broker import PaperBroker
from angel_auto.core.enums import Direction, ExitReason, OptionType, OrderSide, PositionStatus, StructureType
from angel_auto.data.market_data import OptionChainSnapshot
from angel_auto.oms.order_manager import OrderManager
from angel_auto.persistence import journal
from angel_auto.strategy.base import EntryIntent, ExitIntent, LegIntent

LOT_SIZE = 65


def _leg(role: str, side: OrderSide, token: str, opt_type=OptionType.CE, strike=24600.0) -> LegIntent:
    return LegIntent(
        option_type=opt_type, strike=strike, role=role, side=side, token=token,
        trading_symbol=f"NIFTY18AUG26{int(strike)}{opt_type.value}", quantity=LOT_SIZE, delta_at_selection=0.5,
    )


def _chain_with_quotes(quotes: dict[str, float]) -> OptionChainSnapshot:
    chain = OptionChainSnapshot()
    for token, ltp in quotes.items():
        chain.register(token, f"SYM{token}", 24600.0, "CE")
        chain.update_ltp(token, ltp)
    return chain


# --- Entry sequencing --------------------------------------------------------


def test_debit_entry_fills_both_legs_itm_first():
    chain = _chain_with_quotes({"ITM": 300.0, "OTM": 65.0})
    broker = PaperBroker(chain, starting_capital_rs=100000)
    oms = OrderManager(broker, entry_slippage_buffer_pts=1.0)

    intent = EntryIntent(
        direction=Direction.LONG, structure_type=StructureType.DEBIT, expiry="29SEP2026",
        legs=[_leg("ITM", OrderSide.BUY, "ITM"), _leg("OTM", OrderSide.SELL, "OTM")],
    )

    position_id = oms.execute_entry(intent)
    assert position_id is not None

    open_position = journal.get_open_position()
    assert open_position["status"] == PositionStatus.OPEN
    itm = next(leg for leg in open_position["legs"] if leg["role"] == "ITM")
    otm = next(leg for leg in open_position["legs"] if leg["role"] == "OTM")
    assert itm["entry_price"] is not None
    assert otm["entry_price"] is not None

    daily_state = journal.get_or_create_daily_state()
    assert daily_state["trades_taken"] == 1


def test_credit_entry_fills_hedge_first():
    chain = _chain_with_quotes({"ITM": 300.0, "OTM": 65.0})
    broker = PaperBroker(chain, starting_capital_rs=100000)
    oms = OrderManager(broker, entry_slippage_buffer_pts=1.0)

    intent = EntryIntent(
        direction=Direction.LONG, structure_type=StructureType.CREDIT, expiry="20AUG2026",
        legs=[_leg("ITM", OrderSide.SELL, "ITM"), _leg("OTM", OrderSide.BUY, "OTM")],
    )

    position_id = oms.execute_entry(intent)
    assert position_id is not None

    # verify the hedge (OTM) order was created before the ITM order, by ordering of ids
    open_position = journal.get_open_position()
    itm_leg_id = next(leg["id"] for leg in open_position["legs"] if leg["role"] == "ITM")
    otm_leg_id = next(leg["id"] for leg in open_position["legs"] if leg["role"] == "OTM")
    assert otm_leg_id < itm_leg_id  # hedge leg row created first


def test_entry_aborted_when_first_leg_has_no_quote():
    chain = _chain_with_quotes({"OTM": 65.0})  # ITM token has no quote
    broker = PaperBroker(chain, starting_capital_rs=100000)
    oms = OrderManager(broker)

    intent = EntryIntent(
        direction=Direction.LONG, structure_type=StructureType.DEBIT, expiry="29SEP2026",
        legs=[_leg("ITM", OrderSide.BUY, "ITM"), _leg("OTM", OrderSide.SELL, "OTM")],
    )

    position_id = oms.execute_entry(intent)
    assert position_id is None
    assert journal.get_open_position() is None

    daily_state = journal.get_or_create_daily_state()
    assert daily_state["trades_taken"] == 0  # never counted - entry never really happened


def test_credit_entry_unwinds_hedge_when_itm_short_fails():
    # hedge (OTM) has a quote and fills, but ITM has none -> emergency-exit the hedge
    chain = _chain_with_quotes({"OTM": 65.0})
    broker = PaperBroker(chain, starting_capital_rs=100000)
    oms = OrderManager(broker)

    intent = EntryIntent(
        direction=Direction.LONG, structure_type=StructureType.CREDIT, expiry="20AUG2026",
        legs=[_leg("ITM", OrderSide.SELL, "ITM"), _leg("OTM", OrderSide.BUY, "OTM")],
    )

    position_id = oms.execute_entry(intent)
    assert position_id is None
    assert journal.get_open_position() is None

    # the hedge leg should show an exit_price (it was bought then immediately sold back)
    # position is aborted, not open/closed
    from angel_auto.persistence.db import session_scope
    from angel_auto.persistence.models import Position

    with session_scope() as session:
        positions = session.query(Position).all()
        assert len(positions) == 1
        assert positions[0].status == PositionStatus.ABORTED


def test_debit_entry_leaves_itm_standalone_when_otm_keeps_failing():
    chain = _chain_with_quotes({"ITM": 300.0})  # OTM never has a quote
    broker = PaperBroker(chain, starting_capital_rs=100000)
    oms = OrderManager(broker, max_otm_retry_attempts=2, retry_delay_sec=0.01)

    intent = EntryIntent(
        direction=Direction.LONG, structure_type=StructureType.DEBIT, expiry="29SEP2026",
        legs=[_leg("ITM", OrderSide.BUY, "ITM"), _leg("OTM", OrderSide.SELL, "OTM")],
    )

    position_id = oms.execute_entry(intent)
    assert position_id is not None  # still opened - ITM alone is a safe position

    open_position = journal.get_open_position()
    assert open_position["status"] == PositionStatus.OPEN
    itm = next(leg for leg in open_position["legs"] if leg["role"] == "ITM")
    otm = next(leg for leg in open_position["legs"] if leg["role"] == "OTM")
    assert itm["entry_price"] is not None
    assert otm["entry_price"] is None  # never filled


# --- Exit --------------------------------------------------------------------


def test_execute_exit_closes_position_and_records_pnl():
    chain = _chain_with_quotes({"ITM": 300.0, "OTM": 65.0})
    broker = PaperBroker(chain, starting_capital_rs=100000)
    oms = OrderManager(broker)

    intent = EntryIntent(
        direction=Direction.LONG, structure_type=StructureType.DEBIT, expiry="29SEP2026",
        legs=[_leg("ITM", OrderSide.BUY, "ITM"), _leg("OTM", OrderSide.SELL, "OTM")],
    )
    oms.execute_entry(intent)

    # move price up nicely for the long ITM leg before exiting
    chain.update_ltp("ITM", 350.0)

    oms.execute_exit(ExitIntent(reason=ExitReason.FIXED_SL), daily_loss_limit_rs=8000, max_consecutive_losses=5)

    assert journal.get_open_position() is None
    daily_state = journal.get_or_create_daily_state()
    assert daily_state["realized_pnl_rs"] != 0.0


def test_execute_exit_triggers_circuit_breaker_on_big_loss():
    chain = _chain_with_quotes({"ITM": 300.0, "OTM": 65.0})
    broker = PaperBroker(chain, starting_capital_rs=100000)
    oms = OrderManager(broker)

    intent = EntryIntent(
        direction=Direction.LONG, structure_type=StructureType.DEBIT, expiry="29SEP2026",
        legs=[_leg("ITM", OrderSide.BUY, "ITM"), _leg("OTM", OrderSide.SELL, "OTM")],
    )
    oms.execute_entry(intent)

    chain.update_ltp("ITM", 100.0)  # big drop -> big loss on exit
    oms.execute_exit(ExitIntent(reason=ExitReason.FIXED_SL), daily_loss_limit_rs=1.0, max_consecutive_losses=5)

    daily_state = journal.get_or_create_daily_state()
    assert daily_state["trading_halted"] is True


def test_execute_exit_noop_when_nothing_open():
    chain = _chain_with_quotes({})
    broker = PaperBroker(chain, starting_capital_rs=100000)
    oms = OrderManager(broker)

    # should not raise
    oms.execute_exit(ExitIntent(reason=ExitReason.MANUAL_EXIT), daily_loss_limit_rs=8000, max_consecutive_losses=5)
    assert journal.get_open_position() is None

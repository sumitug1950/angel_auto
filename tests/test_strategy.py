from datetime import date, datetime, timedelta, timezone

import pytest

from angel_auto.core.enums import Direction, ExitReason, OptionType, OrderSide, StructureType
from angel_auto.data.instruments import Instrument, InstrumentMaster
from angel_auto.data.market_data import BarAggregator, OptionChainSnapshot
from angel_auto.persistence import journal
from angel_auto.settings import StrategyConfig
from angel_auto.strategy.base import EntryIntent, ExitIntent
from angel_auto.strategy.macd_itm_otm_spread import MacdItmOtmSpreadStrategy

LOT_SIZE = 65
UNDERLYING = "NIFTY"


def _fake_instruments(expiries: list[str], strikes: list[float]) -> InstrumentMaster:
    """A monthly expiry (last of each month) and a nearest/current expiry, both offering
    the same CE/PE strikes on a clean 100-pt grid - enough for strike-selection tests."""
    master = InstrumentMaster()
    by_key: dict[tuple[str, str, str], list[Instrument]] = {}
    token = 1000
    for expiry in expiries:
        instruments = []
        for strike in strikes:
            for opt_type in ("CE", "PE"):
                instruments.append(
                    Instrument(
                        token=str(token),
                        symbol=f"NIFTY{expiry}{int(strike)}{opt_type}",
                        name=UNDERLYING,
                        expiry=expiry,
                        strike=strike,
                        lot_size=LOT_SIZE,
                        instrument_type="OPTIDX",
                        exchange="NFO",
                    )
                )
                token += 1
        by_key[(UNDERLYING, "OPTIDX", expiry)] = instruments
    master._by_name_type_expiry = by_key
    master._loaded = True
    return master


def _seed_option_chain(instruments: InstrumentMaster, expiry: str, strikes: list[float], deltas: dict) -> OptionChainSnapshot:
    """deltas: {(strike, "CE"|"PE"): delta_value}. Registers + sets ltp/delta for each."""
    chain = OptionChainSnapshot()
    for inst in instruments.option_chain(UNDERLYING, expiry):
        chain.register(inst.token, inst.symbol, inst.strike, inst.symbol[-2:])
        key = (inst.strike, inst.symbol[-2:])
        if key in deltas:
            chain.update_ltp(inst.token, 100.0)  # any positive placeholder price
            quote = chain.get(inst.token)
            quote.iv = 0.14
            quote.delta = deltas[key]
    return chain


def _default_config() -> StrategyConfig:
    return StrategyConfig(class_path="angel_auto.strategy.macd_itm_otm_spread.MacdItmOtmSpreadStrategy")


def _bullish_bars(interval_sec: int = 15, n: int = 60) -> BarAggregator:
    bars = BarAggregator(interval_sec=interval_sec)
    base = datetime(2026, 8, 17, 9, 15, tzinfo=timezone.utc)
    prices = [24700 + i * 3 for i in range(n)]  # steady uptrend -> ends BULLISH
    for i, price in enumerate(prices):
        bars.add_tick(price, base + timedelta(seconds=i * interval_sec))
    return bars


def _bearish_bars(interval_sec: int = 15, n: int = 60) -> BarAggregator:
    bars = BarAggregator(interval_sec=interval_sec)
    base = datetime(2026, 8, 17, 9, 15, tzinfo=timezone.utc)
    prices = [25100 - i * 3 for i in range(n)]  # steady downtrend -> ends BEARISH
    for i, price in enumerate(prices):
        bars.add_tick(price, base + timedelta(seconds=i * interval_sec))
    return bars


def _make_strategy(config, instruments, bars, chain, vix=20.0, max_trades_per_day=2):
    return MacdItmOtmSpreadStrategy(
        config=config,
        underlying=UNDERLYING,
        lot_size=LOT_SIZE,
        max_trades_per_day=max_trades_per_day,
        instruments=instruments,
        bar_aggregator=bars,
        option_chain=chain,
        get_current_vix=lambda: vix,
    )


STRIKES = [24400.0, 24500.0, 24600.0, 24700.0, 24800.0, 24900.0, 25000.0, 25100.0, 25200.0]
# delta ~0.7 at 24600 (ITM for CE), ~0.1 at 25200 (OTM for CE); mirrored for PE
CE_DELTAS = {
    (24400.0, "CE"): 0.85, (24500.0, "CE"): 0.78, (24600.0, "CE"): 0.70, (24700.0, "CE"): 0.55,
    (24800.0, "CE"): 0.40, (24900.0, "CE"): 0.25, (25000.0, "CE"): 0.15, (25100.0, "CE"): 0.10,
    (25200.0, "CE"): 0.05,
}
PE_DELTAS = {
    (24400.0, "PE"): -0.05, (24500.0, "PE"): -0.10, (24600.0, "PE"): -0.15, (24700.0, "PE"): -0.25,
    (24800.0, "PE"): -0.40, (24900.0, "PE"): -0.55, (25000.0, "PE"): -0.70, (25100.0, "PE"): -0.78,
    (25200.0, "PE"): -0.85,
}
ALL_DELTAS = {**CE_DELTAS, **PE_DELTAS}


# --- Direction request / MACD gating -----------------------------------------


def test_direction_request_executes_immediately_when_macd_already_matches():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    intent = strategy.on_direction_request(Direction.LONG)

    assert isinstance(intent, EntryIntent)
    assert intent.direction == Direction.LONG
    assert journal.get_pending_direction_request() is None  # resolved, not left pending


def test_direction_request_goes_pending_when_macd_disagrees():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bearish_bars()  # MACD will be BEARISH
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    intent = strategy.on_direction_request(Direction.LONG)  # asking for LONG while bearish

    assert intent is None
    pending = journal.get_pending_direction_request()
    assert pending is not None
    assert pending["direction"] == Direction.LONG


def test_new_direction_request_replaces_existing_pending():
    # market state is BEARISH throughout - a LONG request always goes Pending here, so a
    # second LONG click (changed your mind about timing, not direction) should replace the
    # first pending request rather than stacking a second one.
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bearish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    strategy.on_direction_request(Direction.LONG)
    first_pending_id = journal.get_pending_direction_request()["id"]

    strategy.on_direction_request(Direction.LONG)
    second_pending = journal.get_pending_direction_request()

    assert second_pending is not None
    assert second_pending["id"] != first_pending_id
    assert second_pending["direction"] == Direction.LONG


def test_cancel_pending_request():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bearish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    strategy.on_direction_request(Direction.LONG)
    assert strategy.cancel_pending_request() is True
    assert journal.get_pending_direction_request() is None
    assert strategy.cancel_pending_request() is False  # nothing left to cancel


def test_on_market_data_does_nothing_with_pending_and_no_crossover():
    # flat prices -> no crossover ever happens
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = BarAggregator(interval_sec=15)
    base = datetime(2026, 8, 17, 9, 15, tzinfo=timezone.utc)
    for i in range(30):
        bars.add_tick(24800.0, base + timedelta(seconds=i * 15))
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    journal.create_direction_request(Direction.LONG, macd_state_at_request="BEARISH")
    result = strategy.on_market_data()
    assert result is None
    assert journal.get_pending_direction_request() is not None


def test_max_trades_per_day_blocks_entry():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain, max_trades_per_day=2)

    journal.increment_daily_trade_count()
    journal.increment_daily_trade_count()  # already at the cap

    intent = strategy.on_direction_request(Direction.LONG)
    assert intent is None
    # request should be cancelled outright, not left pending
    assert journal.get_pending_direction_request() is None


def test_trading_halt_blocks_entry():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    journal.set_trading_halt("daily loss limit hit")
    intent = strategy.on_direction_request(Direction.LONG)
    assert intent is None


# --- Structure / expiry / option-type selection -------------------------------


def test_structure_defaults_to_debit_when_iv_rank_low():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    for d in range(1, 91):
        journal.upsert_vix_close(date(2026, 5, 1) + timedelta(days=d), 20.0)  # flat history -> rank ~0 at vix=20...
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=15.0)  # low vs any real spread

    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.DEBIT
    assert intent.expiry == monthly  # DEBIT -> monthly


def test_structure_switches_to_credit_when_iv_rank_high():
    nearest = "20AUG2026"
    monthly = "29SEP2026"
    instruments = _fake_instruments([nearest, monthly], STRIKES)
    chain = _seed_option_chain(instruments, nearest, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()

    for d in range(90):
        journal.upsert_vix_close(date(2026, 5, 1) + timedelta(days=d), 10.0 + (d % 20))  # range 10-29
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=29.0)  # near top of range -> high rank

    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.CREDIT
    assert intent.expiry == nearest  # CREDIT -> nearest/current, not monthly


@pytest.mark.parametrize(
    "direction,structure,expected_type",
    [
        (Direction.LONG, StructureType.DEBIT, OptionType.CE),
        (Direction.SHORT, StructureType.DEBIT, OptionType.PE),
    ],
)
def test_option_type_mapping_debit(direction, structure, expected_type):
    assert MacdItmOtmSpreadStrategy._option_type_for(direction, structure) == expected_type


@pytest.mark.parametrize(
    "direction,structure,expected_type",
    [
        (Direction.LONG, StructureType.CREDIT, OptionType.PE),
        (Direction.SHORT, StructureType.CREDIT, OptionType.CE),
    ],
)
def test_option_type_mapping_credit(direction, structure, expected_type):
    assert MacdItmOtmSpreadStrategy._option_type_for(direction, structure) == expected_type


# --- Strike selection ----------------------------------------------------


def test_strike_selection_picks_closest_delta_and_correct_sides_for_debit():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=10.0)  # keep IV rank low -> DEBIT

    for d in range(90):
        journal.upsert_vix_close(date(2026, 5, 1) + timedelta(days=d), 15.0)

    intent = strategy.on_direction_request(Direction.LONG)  # DEBIT -> CE
    itm = next(leg for leg in intent.legs if leg.role == "ITM")
    otm = next(leg for leg in intent.legs if leg.role == "OTM")

    assert itm.strike == 24600.0  # delta 0.70 exactly
    assert otm.strike == 25100.0  # delta 0.10 exactly
    assert itm.side == OrderSide.BUY
    assert otm.side == OrderSide.SELL


def test_strike_selection_sides_flip_for_credit():
    nearest = "20AUG2026"
    instruments = _fake_instruments([nearest], STRIKES)
    chain = _seed_option_chain(instruments, nearest, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()

    for d in range(90):
        journal.upsert_vix_close(date(2026, 5, 1) + timedelta(days=d), 10.0 + (d % 20))
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=29.0)  # -> CREDIT

    intent = strategy.on_direction_request(Direction.LONG)  # CREDIT -> PE
    assert intent.structure_type == StructureType.CREDIT
    itm = next(leg for leg in intent.legs if leg.role == "ITM")
    otm = next(leg for leg in intent.legs if leg.role == "OTM")
    assert itm.side == OrderSide.SELL
    assert otm.side == OrderSide.BUY


def test_strike_selection_returns_none_without_live_quotes():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = OptionChainSnapshot()  # nothing registered/priced
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    intent = strategy.on_direction_request(Direction.LONG)
    assert intent is None
    # request should have been cancelled, not left pending, since MACD did match
    assert journal.get_pending_direction_request() is None


# --- Exit logic ------------------------------------------------------------


def _open_test_position(instruments, chain, expiry, direction=Direction.LONG) -> dict:
    position_id = journal.create_position(direction, StructureType.DEBIT, expiry)
    itm_token, otm_token = None, None
    for inst in instruments.option_chain(UNDERLYING, expiry):
        if inst.strike == 24600.0 and inst.symbol.endswith("CE"):
            itm_token = inst.token
        if inst.strike == 25100.0 and inst.symbol.endswith("CE"):
            otm_token = inst.token
    itm_leg_id = journal.add_leg(position_id, itm_token, "ITM_CE", OptionType.CE, 24600.0, "ITM", OrderSide.BUY, LOT_SIZE)
    otm_leg_id = journal.add_leg(position_id, otm_token, "OTM_CE", OptionType.CE, 25100.0, "OTM", OrderSide.SELL, LOT_SIZE)
    journal.update_leg_fill(itm_leg_id, entry_price=300.0)
    journal.update_leg_fill(otm_leg_id, entry_price=65.0)
    journal.update_position_status(position_id, journal.PositionStatus.OPEN, set_entry_time=True)
    chain.update_ltp(itm_token, 300.0)
    chain.update_ltp(otm_token, 65.0)
    return {"position_id": position_id, "itm_token": itm_token, "otm_token": otm_token}


def test_fixed_sl_triggers():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    ids = _open_test_position(instruments, chain, monthly)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    # net premium moves against a long-ITM/short-OTM debit spread when ITM drops a lot
    chain.update_ltp(ids["itm_token"], 300.0 - (4200 / LOT_SIZE))  # ITM leg down enough to exceed 4000 SL
    chain.update_ltp(ids["otm_token"], 65.0)

    result = strategy.on_market_data()
    assert isinstance(result, ExitIntent)
    assert result.reason == ExitReason.FIXED_SL


def test_trailing_stop_locks_in_profit_above_target():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    ids = _open_test_position(instruments, chain, monthly)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    # push profit to 7000 (above the 4800 target) -> trailing activates, peak=7000, stop=6000
    chain.update_ltp(ids["itm_token"], 300.0 + (7000 / LOT_SIZE))
    result = strategy.on_market_data()
    assert result is None  # trailing, not exited yet

    open_position = journal.get_open_position()
    assert open_position["trail_active"] is True
    assert open_position["peak_profit_rs"] == pytest.approx(7000.0, abs=1.0)

    # pull back to 5500 -> still above the 6000 trailing stop? no, 5500 < 6000 -> should exit
    chain.update_ltp(ids["itm_token"], 300.0 + (5500 / LOT_SIZE))
    result = strategy.on_market_data()
    assert isinstance(result, ExitIntent)
    assert result.reason == ExitReason.TRAILING_STOP


def test_opposite_macd_does_not_exit_by_default():
    # MACD only gates entry timing now - a position stays open through a reversal unless
    # SL/target/manual/square-off says otherwise (exit_on_opposite_macd defaults to False).
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    ids = _open_test_position(instruments, chain, monthly, direction=Direction.LONG)
    bars = _bearish_bars()  # opposite of the LONG position
    config = _default_config()
    assert config.exit.exit_on_opposite_macd is False
    strategy = _make_strategy(config, instruments, bars, chain)

    # small P&L, nowhere near SL or target
    chain.update_ltp(ids["itm_token"], 305.0)

    result = strategy.on_market_data()
    assert result is None


def test_opposite_macd_exit_fires_when_explicitly_enabled():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    ids = _open_test_position(instruments, chain, monthly, direction=Direction.LONG)
    bars = _bearish_bars()  # opposite of the LONG position
    config = _default_config()
    config.exit.exit_on_opposite_macd = True
    strategy = _make_strategy(config, instruments, bars, chain)

    chain.update_ltp(ids["itm_token"], 305.0)

    result = strategy.on_market_data()
    assert isinstance(result, ExitIntent)
    assert result.reason == ExitReason.OPPOSITE_MACD


def test_manual_exit_returns_none_when_nothing_open():
    instruments = _fake_instruments(["29SEP2026"], STRIKES)
    chain = OptionChainSnapshot()
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)
    assert strategy.manual_exit() is None


def test_manual_exit_force_closes_regardless_of_pnl():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    _open_test_position(instruments, chain, monthly)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    result = strategy.manual_exit()
    assert isinstance(result, ExitIntent)
    assert result.reason == ExitReason.MANUAL_EXIT


def test_square_off_trigger():
    monthly = "29SEP2026"
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    _open_test_position(instruments, chain, monthly)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    result = strategy.on_square_off_trigger()
    assert isinstance(result, ExitIntent)
    assert result.reason == ExitReason.SQUARE_OFF

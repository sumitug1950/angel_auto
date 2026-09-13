from datetime import date, datetime, timedelta, timezone

import pytest

from angel_auto.core.enums import Direction, ExitReason, LevelOrderStatus, OptionType, OrderSide, StructureType
from angel_auto.data.instruments import Instrument, InstrumentMaster
from angel_auto.data.market_data import BarAggregator, OptionChainSnapshot
from angel_auto.persistence import journal
from angel_auto.settings import StrategyConfig
from angel_auto.strategy.base import EntryIntent, ExitIntent
from angel_auto.strategy.macd_itm_otm_spread import MacdItmOtmSpreadStrategy

LOT_SIZE = 65
UNDERLYING = "NIFTY"


def _expiry(days_ahead: int) -> str:
    return (date.today() + timedelta(days=days_ahead)).strftime("%d%b%Y").upper()


# Relative to today, not hardcoded - the strategy's expiry lookups filter against date.today(),
# so a fixed date silently breaks these tests once it passes.
NEAREST = _expiry(3)
MONTHLY = _expiry(30)


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
        chain.register(inst.token, inst.symbol, inst.strike, inst.symbol[-2:], expiry=expiry)
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


def _make_strategy(config, instruments, bars, chain, vix=20.0, max_trades_per_day=2, expiry="first"):
    """`expiry` is the dashboard pick: "first" = the fake master's first listed expiry, None = none picked."""
    if expiry == "first":
        expiry = instruments.available_expiries(UNDERLYING)[0]
    if expiry is not None:
        journal.set_expiry_preference(expiry)
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
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    intent = strategy.on_direction_request(Direction.LONG)

    assert isinstance(intent, EntryIntent)
    assert intent.direction == Direction.LONG
    assert journal.get_pending_direction_request() is None  # resolved, not left pending


def test_direction_request_goes_pending_when_macd_disagrees():
    monthly = MONTHLY
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
    monthly = MONTHLY
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
    monthly = MONTHLY
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
    monthly = MONTHLY
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
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain, max_trades_per_day=2)

    journal.increment_daily_trade_count()
    journal.increment_daily_trade_count()  # already at the cap

    intent = strategy.on_direction_request(Direction.LONG)
    assert intent is None
    # request should be cancelled outright, not left pending - and say why on the dashboard
    assert journal.get_pending_direction_request() is None
    assert "daily trade cap" in strategy.entry_notice["message"]


def test_trading_halt_blocks_entry():
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    journal.set_trading_halt("daily loss limit hit")
    intent = strategy.on_direction_request(Direction.LONG)
    assert intent is None


# --- Structure / expiry / option-type selection -------------------------------


def test_structure_defaults_to_debit_when_no_preference_ever_set():
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=15.0)
    journal.upsert_vix_close(date.today() - timedelta(days=1), 15.0)  # flat -> no spike

    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.DEBIT
    assert intent.expiry == monthly  # DEBIT -> monthly


def test_structure_respects_manual_selling_preference():
    nearest = NEAREST
    monthly = MONTHLY
    instruments = _fake_instruments([nearest, monthly], STRIKES)
    chain = _seed_option_chain(instruments, nearest, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=15.0)
    journal.upsert_vix_close(date.today() - timedelta(days=1), 15.0)  # flat -> no spike

    strategy.on_structure_request(StructureType.CREDIT)  # "Selling" button
    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.CREDIT
    assert intent.expiry == nearest  # CREDIT -> nearest/current, not monthly


def test_vix_spike_up_forces_debit_overriding_selling_preference():
    # "Selling" was pressed (would normally give CREDIT), but VIX jumped >=3% since
    # yesterday - that must force DEBIT regardless of the button.
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    journal.upsert_vix_close(date.today() - timedelta(days=1), 20.0)  # yesterday 20 -> today 29 is a ~45% jump
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=29.0)

    strategy.on_structure_request(StructureType.CREDIT)
    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.DEBIT
    assert intent.expiry == monthly  # DEBIT -> monthly, confirms the override took effect


def test_vix_spike_down_forces_credit_overriding_buying_preference():
    # "Buying" preference (or the default), but VIX dropped >=3% since yesterday - a
    # calming market, so that forces CREDIT regardless of the button.
    nearest = NEAREST
    instruments = _fake_instruments([nearest], STRIKES)
    chain = _seed_option_chain(instruments, nearest, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    journal.upsert_vix_close(date.today() - timedelta(days=1), 40.0)  # yesterday 40 -> today 29 is a big drop
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=29.0)

    strategy.on_structure_request(StructureType.DEBIT)  # "Buying" button
    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.CREDIT  # VIX-drop override wins anyway


def test_vix_change_under_threshold_respects_preference():
    nearest = NEAREST
    instruments = _fake_instruments([nearest], STRIKES)
    chain = _seed_option_chain(instruments, nearest, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    journal.upsert_vix_close(date.today() - timedelta(days=1), 28.3)  # (29-28.3)/28.3 ~= 2.47% - under 3%
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=29.0)

    strategy.on_structure_request(StructureType.CREDIT)
    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.CREDIT  # no override - preference respected


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
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    journal.upsert_vix_close(date.today() - timedelta(days=1), 15.0)  # flat -> no spike, default DEBIT applies
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=15.0)

    intent = strategy.on_direction_request(Direction.LONG)  # DEBIT -> CE
    itm = next(leg for leg in intent.legs if leg.role == "ITM")
    otm = next(leg for leg in intent.legs if leg.role == "OTM")

    assert itm.strike == 24600.0  # delta 0.70 exactly
    assert otm.strike == 25100.0  # delta 0.10 exactly
    assert itm.side == OrderSide.BUY
    assert otm.side == OrderSide.SELL
    assert itm.quantity == LOT_SIZE  # sizing.lots defaults to 1
    assert otm.quantity == LOT_SIZE


def test_strike_selection_sides_flip_for_credit():
    nearest = NEAREST
    instruments = _fake_instruments([nearest], STRIKES)
    chain = _seed_option_chain(instruments, nearest, STRIKES, ALL_DELTAS)
    bars = _bullish_bars()
    journal.upsert_vix_close(date.today() - timedelta(days=1), 15.0)  # flat -> no spike
    strategy = _make_strategy(_default_config(), instruments, bars, chain, vix=15.0)

    strategy.on_structure_request(StructureType.CREDIT)  # "Selling" button
    intent = strategy.on_direction_request(Direction.LONG)  # CREDIT -> PE
    assert intent.structure_type == StructureType.CREDIT
    itm = next(leg for leg in intent.legs if leg.role == "ITM")
    otm = next(leg for leg in intent.legs if leg.role == "OTM")
    assert itm.side == OrderSide.SELL
    assert otm.side == OrderSide.BUY


def test_missing_live_quotes_keeps_request_pending_and_retries():
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, OptionChainSnapshot())  # no quotes yet

    intent = strategy.on_direction_request(Direction.LONG)  # MACD agrees, but nothing to build legs from
    assert intent is None
    pending = journal.get_pending_direction_request()
    assert pending is not None  # NOT cancelled - still waiting
    assert strategy.entry_notice["request_id"] == pending["id"]
    assert "Pending" in strategy.entry_notice["message"]

    assert strategy.on_market_data() is None  # still no quotes - keeps waiting
    assert journal.get_pending_direction_request() is not None

    strategy.option_chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)  # quotes arrive
    intent = strategy.on_market_data()
    assert isinstance(intent, EntryIntent)
    assert intent.direction == Direction.LONG
    assert journal.get_pending_direction_request() is None
    assert strategy.entry_notice is None


def test_pending_request_executes_once_macd_agrees_even_without_crossover_on_last_candle():
    # The crossover happens mid-rally, several candles before on_market_data runs - a
    # same-candle crossover check would miss it entirely; the state check must not.
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    bars = _bearish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)
    assert strategy.on_direction_request(Direction.LONG) is None  # goes Pending

    base = datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc)  # right after _bearish_bars' last candle
    for i in range(40):
        bars.add_tick(24923 + i * 10, base + timedelta(seconds=i * 15))  # sharp rally

    intent = strategy.on_market_data()
    assert isinstance(intent, EntryIntent)
    assert intent.direction == Direction.LONG


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
    monthly = MONTHLY
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
    monthly = MONTHLY
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


def test_exit_rules_wait_for_a_live_quote_on_every_open_leg():
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    ids = _open_test_position(instruments, chain, monthly)
    journal.update_trailing_peak(ids["position_id"], 7000.0, trail_active=True)
    chain.update_ltp(ids["otm_token"], 0.0)  # e.g. just restarted - no tick for this leg yet
    strategy = _make_strategy(_default_config(), instruments, _bullish_bars(), chain)

    # With the OTM leg counted as flat, P&L would read ~0 and fire the persisted trailing stop.
    assert strategy.on_market_data() is None


def test_opposite_macd_does_not_exit_by_default():
    # MACD only gates entry timing now - a position stays open through a reversal unless
    # SL/target/manual/square-off says otherwise (exit_on_opposite_macd defaults to False).
    monthly = MONTHLY
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
    monthly = MONTHLY
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
    instruments = _fake_instruments([MONTHLY], STRIKES)
    chain = OptionChainSnapshot()
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)
    assert strategy.manual_exit() is None


def test_manual_exit_force_closes_regardless_of_pnl():
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    _open_test_position(instruments, chain, monthly)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    result = strategy.manual_exit()
    assert isinstance(result, ExitIntent)
    assert result.reason == ExitReason.MANUAL_EXIT


def test_square_off_trigger():
    monthly = MONTHLY
    instruments = _fake_instruments([monthly], STRIKES)
    chain = _seed_option_chain(instruments, monthly, STRIKES, ALL_DELTAS)
    _open_test_position(instruments, chain, monthly)
    bars = _bullish_bars()
    strategy = _make_strategy(_default_config(), instruments, bars, chain)

    result = strategy.on_square_off_trigger()
    assert isinstance(result, ExitIntent)
    assert result.reason == ExitReason.SQUARE_OFF


# --- Config-driven rules (strategies.yaml buying / selling / vix_override / macd) ----------


def test_either_button_trades_the_expiry_picked_on_the_dashboard():
    instruments = _fake_instruments([NEAREST, MONTHLY], STRIKES)
    chain = _seed_option_chain(instruments, MONTHLY, STRIKES, ALL_DELTAS)
    strategy = _make_strategy(_default_config(), instruments, _bullish_bars(), chain, vix=15.0, expiry=MONTHLY)

    strategy.on_structure_request(StructureType.CREDIT)  # Selling - no longer tied to the nearest expiry
    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.CREDIT
    assert intent.expiry == MONTHLY


def test_no_expiry_picked_keeps_the_request_pending_until_one_is():
    instruments = _fake_instruments([NEAREST, MONTHLY], STRIKES)
    chain = _seed_option_chain(instruments, NEAREST, STRIKES, ALL_DELTAS)
    strategy = _make_strategy(_default_config(), instruments, _bullish_bars(), chain, vix=15.0, expiry=None)

    assert strategy.on_direction_request(Direction.LONG) is None
    assert journal.get_pending_direction_request() is not None
    assert "expiry" in strategy.entry_notice["message"].lower()

    assert strategy.on_expiry_request(NEAREST) is True
    intent = strategy.on_market_data()
    assert isinstance(intent, EntryIntent)
    assert intent.expiry == NEAREST


def test_expiry_choices_are_the_next_few_upcoming_expiries():
    past, later, far = _expiry(-1), _expiry(10), _expiry(45)
    instruments = _fake_instruments([past, NEAREST, MONTHLY, later, far], STRIKES)
    config = _default_config()
    config.expiry_choices = 3
    strategy = _make_strategy(config, instruments, _bullish_bars(), OptionChainSnapshot(), expiry=None)

    assert [choice["expiry"] for choice in strategy.expiry_choices()] == [NEAREST, later, MONTHLY]
    assert strategy.on_expiry_request(past) is False  # expired - not on offer
    assert strategy.on_expiry_request(far) is False  # beyond the offered few
    assert strategy.selected_expiry() is None


def test_backtest_expiry_override_trades_without_touching_the_saved_pick():
    instruments = _fake_instruments([NEAREST, MONTHLY], STRIKES)
    chain = _seed_option_chain(instruments, MONTHLY, STRIKES, ALL_DELTAS)
    strategy = _make_strategy(_default_config(), instruments, _bullish_bars(), chain, vix=15.0, expiry=None)
    strategy.expiry_override = MONTHLY

    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.expiry == MONTHLY
    assert journal.get_expiry_preference() is None


# --- Nifty level orders + Nifty SL/target ---------------------------------------------


def _level_strategy(bars=None, with_quotes=True):
    instruments = _fake_instruments([MONTHLY], STRIKES)
    chain = _seed_option_chain(instruments, MONTHLY, STRIKES, ALL_DELTAS) if with_quotes else OptionChainSnapshot()
    strategy = _make_strategy(_default_config(), instruments, bars or _bearish_bars(), chain, vix=15.0, expiry=MONTHLY)
    return strategy, instruments, chain


def test_level_order_enters_as_soon_as_nifty_reaches_the_level_without_waiting_for_macd():
    strategy, _, _ = _level_strategy(bars=_bearish_bars())  # MACD disagrees with a market-upar trade
    order_id = strategy.place_level_order(
        Direction.LONG, StructureType.DEBIT, 24850.0, spot=24800.0, spot_sl=24790.0, spot_target=24950.0
    )

    assert strategy.on_spot_price(24849.0) is None
    intent = strategy.on_spot_price(24851.0)

    assert isinstance(intent, EntryIntent)
    assert (intent.direction, intent.structure_type, intent.expiry) == (Direction.LONG, StructureType.DEBIT, MONTHLY)
    assert {leg.option_type for leg in intent.legs} == {OptionType.CE}
    assert (intent.spot_sl, intent.spot_target, intent.level_order_id) == (24790.0, 24950.0, order_id)
    assert journal.get_active_level_order()["status"] == LevelOrderStatus.TRIGGERED


def test_level_below_nifty_triggers_on_the_way_down():
    strategy, _, _ = _level_strategy(bars=_bullish_bars())
    strategy.place_level_order(Direction.SHORT, StructureType.CREDIT, 24700.0, spot=24800.0)

    assert strategy.on_spot_price(24760.0) is None
    intent = strategy.on_spot_price(24699.0)

    itm = next(leg for leg in intent.legs if leg.role == "ITM")
    assert (intent.structure_type, itm.option_type, itm.side) == (StructureType.CREDIT, OptionType.CE, OrderSide.SELL)  # CALL becho


def test_level_order_rejects_levels_on_the_wrong_side_or_too_close():
    strategy, _, _ = _level_strategy()
    with pytest.raises(ValueError, match="SL"):
        strategy.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot=24800.0, spot_sl=24900.0)
    with pytest.raises(ValueError, match="target"):
        strategy.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot=24800.0, spot_target=24800.0)
    with pytest.raises(ValueError, match="SL"):
        strategy.place_level_order(Direction.SHORT, StructureType.DEBIT, 24700.0, spot=24800.0, spot_sl=24650.0)
    with pytest.raises(ValueError, match="paas"):
        strategy.place_level_order(Direction.LONG, StructureType.DEBIT, 24801.0, spot=24800.0)
    assert journal.get_active_level_order() is None


def test_level_order_needs_an_expiry_pick():
    instruments = _fake_instruments([MONTHLY], STRIKES)
    strategy = _make_strategy(_default_config(), instruments, _bullish_bars(), OptionChainSnapshot(), expiry=None)
    with pytest.raises(ValueError, match="expiry"):
        strategy.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot=24800.0)


def test_new_level_order_replaces_the_active_one_and_cancel_removes_it():
    strategy, _, _ = _level_strategy()
    first = strategy.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot=24800.0)
    second = strategy.place_level_order(Direction.SHORT, StructureType.DEBIT, 24700.0, spot=24800.0)

    assert journal.get_level_order(first)["status"] == LevelOrderStatus.CANCELLED
    assert journal.get_active_level_order()["id"] == second
    assert strategy.cancel_level_order() is True
    assert journal.get_active_level_order() is None


def test_waiting_level_order_can_be_moved_but_not_once_the_level_is_hit():
    strategy, _, _ = _level_strategy()
    order_id = strategy.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot=24800.0, spot_sl=24820.0)

    strategy.modify_level_order(24760.0, spot=24800.0, spot_sl=24740.0, spot_target=24790.0)  # dragged below Nifty
    order = journal.get_level_order(order_id)
    assert (order["trigger_price"], order["trigger_when"], order["spot_sl"], order["spot_target"]) == (
        24760.0, "FALLS_TO", 24740.0, 24790.0,
    )
    assert (order["direction"], order["structure_type"], order["expiry"]) == (Direction.LONG, StructureType.DEBIT, MONTHLY)

    with pytest.raises(ValueError, match="SL"):
        strategy.modify_level_order(24760.0, spot=24800.0, spot_sl=24770.0)

    assert isinstance(strategy.on_spot_price(24759.0), EntryIntent)  # level hit
    with pytest.raises(ValueError, match="chhu chuka"):
        strategy.modify_level_order(24700.0, spot=24759.0)


def test_level_order_is_valid_only_for_the_day_it_was_placed():
    strategy, _, _ = _level_strategy()
    order_id = strategy.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot=24800.0)
    strategy.get_today = lambda: date.today() + timedelta(days=1)

    assert strategy.on_spot_price(24900.0) is None
    assert journal.get_level_order(order_id)["status"] == LevelOrderStatus.EXPIRED


def test_level_hit_while_a_position_is_open_cancels_the_level_order():
    strategy, instruments, chain = _level_strategy()
    order_id = strategy.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot=24800.0)
    _open_test_position(instruments, chain, MONTHLY)

    assert strategy.on_spot_price(24860.0) is None
    assert journal.get_level_order(order_id)["status"] == LevelOrderStatus.CANCELLED


def test_level_entry_waits_for_option_prices_then_gives_up():
    strategy, _, _ = _level_strategy(with_quotes=False)
    order_id = strategy.place_level_order(Direction.LONG, StructureType.DEBIT, 24850.0, spot=24800.0)

    assert strategy.on_spot_price(24851.0) is None
    assert journal.get_level_order(order_id)["status"] == LevelOrderStatus.TRIGGERED
    assert "intezaar" in strategy.entry_notice["message"]

    later = datetime.now(timezone.utc) + timedelta(seconds=strategy.config.level_order.entry_retry_sec + 1)
    strategy.get_now = lambda: later
    assert strategy.on_spot_price(24851.0) is None
    assert journal.get_level_order(order_id)["status"] == LevelOrderStatus.FAILED


def test_nifty_sl_and_target_close_a_market_upar_position():
    strategy, instruments, chain = _level_strategy()
    _open_test_position(instruments, chain, MONTHLY, direction=Direction.LONG)
    strategy.set_position_spot_levels(spot=24800.0, spot_sl=24750.0, spot_target=24900.0)

    assert strategy.on_spot_price(24760.0) is None
    assert strategy.on_spot_price(24750.0).reason == ExitReason.SPOT_SL
    assert strategy.on_spot_price(24905.0).reason == ExitReason.SPOT_TARGET


def test_nifty_sl_sits_above_a_market_neeche_position_and_can_be_removed():
    strategy, instruments, chain = _level_strategy()
    _open_test_position(instruments, chain, MONTHLY, direction=Direction.SHORT)
    with pytest.raises(ValueError, match="upar"):
        strategy.set_position_spot_levels(spot=24800.0, spot_sl=24750.0, spot_target=None)
    strategy.set_position_spot_levels(spot=24800.0, spot_sl=24850.0, spot_target=24700.0)

    assert strategy.on_spot_price(24849.0) is None
    assert strategy.on_spot_price(24851.0).reason == ExitReason.SPOT_SL
    assert strategy.on_spot_price(24690.0).reason == ExitReason.SPOT_TARGET

    strategy.set_position_spot_levels(spot=24800.0, spot_sl=None, spot_target=None)
    assert strategy.on_spot_price(24990.0) is None


def test_start_with_selling_applies_until_a_button_is_pressed():
    instruments = _fake_instruments([NEAREST], STRIKES)
    chain = _seed_option_chain(instruments, NEAREST, STRIKES, ALL_DELTAS)
    config = _default_config()
    config.start_with = "SELLING"
    strategy = _make_strategy(config, instruments, _bullish_bars(), chain, vix=15.0)

    assert strategy.structure_preference() == StructureType.CREDIT
    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.CREDIT


def test_vix_override_can_be_switched_off():
    instruments = _fake_instruments([NEAREST], STRIKES)
    chain = _seed_option_chain(instruments, NEAREST, STRIKES, ALL_DELTAS)
    journal.upsert_vix_close(date.today() - timedelta(days=1), 20.0)  # 20 -> 29 is a big spike up
    config = _default_config()
    config.vix_override.on_rise = "OFF"
    strategy = _make_strategy(config, instruments, _bullish_bars(), chain, vix=29.0)

    strategy.on_structure_request(StructureType.CREDIT)
    intent = strategy.on_direction_request(Direction.LONG)
    assert intent.structure_type == StructureType.CREDIT  # spike ignored - the button is respected


def test_strike_grid_and_delta_targets_come_from_the_button_block():
    instruments = _fake_instruments([MONTHLY], STRIKES)
    chain = _seed_option_chain(instruments, MONTHLY, STRIKES, ALL_DELTAS)
    config = _default_config()
    config.buying.strike_grid = 200  # only 24400, 24600, 24800, 25000, 25200
    config.buying.itm_delta = 0.78   # 24500 (exactly 0.78) is off-grid -> 24400 (0.85) is closest
    config.buying.otm_delta = 0.15
    strategy = _make_strategy(config, instruments, _bullish_bars(), chain, vix=15.0)

    intent = strategy.on_direction_request(Direction.LONG)
    assert {leg.role: leg.strike for leg in intent.legs} == {"ITM": 24400.0, "OTM": 25000.0}


def test_macd_warmup_keeps_request_pending_until_enough_candles():
    instruments = _fake_instruments([MONTHLY], STRIKES)
    chain = _seed_option_chain(instruments, MONTHLY, STRIKES, ALL_DELTAS)
    bars = _bullish_bars(n=60)
    config = _default_config()
    config.macd.min_candles_before_entry = 80
    strategy = _make_strategy(config, instruments, bars, chain, vix=15.0)

    assert strategy.on_direction_request(Direction.LONG) is None  # MACD agrees, but only 60/80 candles
    assert journal.get_pending_direction_request() is not None
    assert "taiyaar" in strategy.entry_notice["message"]
    assert strategy.on_market_data() is None

    base = datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc)  # keep the uptrend going past 80 candles
    for i in range(25):
        bars.add_tick(24880 + i * 3, base + timedelta(seconds=i * 15))

    intent = strategy.on_market_data()
    assert isinstance(intent, EntryIntent)
    assert strategy.entry_notice is None

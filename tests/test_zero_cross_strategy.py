from datetime import date, datetime

import pytest

from angel_auto.core.enums import Direction, ExitReason, OptionType, OrderSide
from angel_auto.data.instruments import Instrument, InstrumentMaster
from angel_auto.data.market_data import OptionChainSnapshot
from angel_auto.persistence import journal
from angel_auto.settings import SingleLegConfig, StrategyConfig
from angel_auto.strategy.base import EntryIntent, ExitIntent
from angel_auto.strategy.macd_zero_cross_single_leg import MacdZeroCrossSingleLegStrategy

LOT_SIZE = 65
UNDERLYING = "NIFTY"
EXPIRY = "20AUG2026"
TODAY = date(2026, 8, 17)
IN_WINDOW = datetime(2026, 8, 17, 10, 0)
OUTSIDE_WINDOW = datetime(2026, 8, 17, 15, 30)


class _FakeMacdEngine:
    """Stands in for TickMacdEngine so strategy-level entry/exit logic can be tested
    without needing a natural price sequence to land an exact zero-side crossover (that
    numeric logic is already covered directly in test_tick_macd.py)."""

    def __init__(self, signal="NONE", side="BULLISH"):
        self.is_warmed_up = True
        self.macd = 1.0
        self.signal = 0.5
        self.histogram = 0.5
        self._next_signal = signal
        self.current_side = side

    def update(self, price):
        return self._next_signal


def _fake_instruments(strikes: list[float]) -> InstrumentMaster:
    master = InstrumentMaster()
    instruments = []
    token = 5000
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            instruments.append(
                Instrument(
                    token=str(token), symbol=f"NIFTY{EXPIRY}{int(strike)}{opt_type}", name=UNDERLYING,
                    expiry=EXPIRY, strike=strike, lot_size=LOT_SIZE, instrument_type="OPTIDX", exchange="NFO",
                )
            )
            token += 1
    master._by_name_type_expiry = {(UNDERLYING, "OPTIDX", EXPIRY): instruments}
    master._loaded = True
    return master


def _seed_option_chain(instruments: InstrumentMaster, strikes: list[float]) -> OptionChainSnapshot:
    chain = OptionChainSnapshot()
    for inst in instruments.option_chain(UNDERLYING, EXPIRY):
        chain.register(inst.token, inst.symbol, inst.strike, inst.symbol[-2:], expiry=EXPIRY)
        chain.update_ltp(inst.token, 100.0)
    return chain


def _config(side: str, itm_offset_count: int = 0) -> StrategyConfig:
    return StrategyConfig(
        class_path="angel_auto.strategy.macd_zero_cross_single_leg.MacdZeroCrossSingleLegStrategy",
        single_leg=SingleLegConfig(side=side, strike_grid=50.0, itm_offset_count=itm_offset_count),
    )


def _make_strategy(name: str, config: StrategyConfig, strikes: list[float], now=IN_WINDOW):
    instruments = _fake_instruments(strikes)
    option_chain = _seed_option_chain(instruments, strikes)
    strategy = MacdZeroCrossSingleLegStrategy(
        strategy_name=name, config=config, underlying=UNDERLYING, lot_size=LOT_SIZE,
        instruments=instruments, option_chain=option_chain, get_today=lambda: TODAY, get_now=lambda: now,
    )
    return strategy


def test_requires_single_leg_config():
    bad_config = StrategyConfig(class_path="angel_auto.strategy.macd_zero_cross_single_leg.MacdZeroCrossSingleLegStrategy")
    with pytest.raises(ValueError):
        MacdZeroCrossSingleLegStrategy(
            strategy_name="x", config=bad_config, underlying=UNDERLYING, lot_size=LOT_SIZE,
            instruments=InstrumentMaster(), option_chain=OptionChainSnapshot(),
        )


def test_manual_trigger_methods_are_no_ops():
    strategy = _make_strategy("atm_sell_macd_zero", _config("SELL"), [24600.0])
    assert strategy.on_direction_request(Direction.LONG) is None
    assert strategy.on_structure_request(None) is None
    assert strategy.cancel_pending_request() is False
    assert strategy.on_market_data() is None


def test_atm_sell_bullish_signal_shorts_atm_put():
    strategy = _make_strategy("atm_sell_macd_zero", _config("SELL"), [24600.0, 24650.0, 24700.0])
    strategy.tick_macd = _FakeMacdEngine(signal="BULLISH")

    intent = strategy.on_tick_price(24650.0)

    assert isinstance(intent, EntryIntent)
    assert intent.direction == Direction.LONG
    leg = intent.legs[0]
    assert leg.option_type == OptionType.PE
    assert leg.side == OrderSide.SELL
    assert leg.strike == 24650.0  # ATM
    assert leg.quantity == LOT_SIZE


def test_atm_sell_bearish_signal_shorts_atm_call():
    strategy = _make_strategy("atm_sell_macd_zero", _config("SELL"), [24600.0, 24650.0, 24700.0])
    strategy.tick_macd = _FakeMacdEngine(signal="BEARISH")

    intent = strategy.on_tick_price(24650.0)

    assert isinstance(intent, EntryIntent)
    assert intent.direction == Direction.SHORT
    leg = intent.legs[0]
    assert leg.option_type == OptionType.CE
    assert leg.side == OrderSide.SELL


def test_itm4_buy_bullish_signal_buys_itm4_call():
    strikes = [24650.0 + i * 50 for i in range(-6, 7)]  # 24350 .. 24950
    strategy = _make_strategy("itm4_buy_macd_zero", _config("BUY", itm_offset_count=4), strikes)
    strategy.tick_macd = _FakeMacdEngine(signal="BULLISH")

    intent = strategy.on_tick_price(24650.0)

    assert isinstance(intent, EntryIntent)
    leg = intent.legs[0]
    assert leg.option_type == OptionType.CE
    assert leg.side == OrderSide.BUY
    assert leg.strike == 24450.0  # ATM(24650) - 4*50, CE ITM is below spot


def test_itm4_buy_bearish_signal_buys_itm4_put():
    strikes = [24650.0 + i * 50 for i in range(-6, 7)]
    strategy = _make_strategy("itm4_buy_macd_zero", _config("BUY", itm_offset_count=4), strikes)
    strategy.tick_macd = _FakeMacdEngine(signal="BEARISH")

    intent = strategy.on_tick_price(24650.0)

    assert isinstance(intent, EntryIntent)
    leg = intent.legs[0]
    assert leg.option_type == OptionType.PE
    assert leg.side == OrderSide.BUY
    assert leg.strike == 24850.0  # ATM(24650) + 4*50, PE ITM is above spot


def test_no_signal_does_not_enter():
    strategy = _make_strategy("atm_sell_macd_zero", _config("SELL"), [24650.0])
    strategy.tick_macd = _FakeMacdEngine(signal="NONE")
    assert strategy.on_tick_price(24650.0) is None


def test_entry_blocked_outside_entry_window():
    strategy = _make_strategy("atm_sell_macd_zero", _config("SELL"), [24650.0], now=OUTSIDE_WINDOW)
    strategy.tick_macd = _FakeMacdEngine(signal="BULLISH")
    assert strategy.on_tick_price(24650.0) is None


def test_not_warmed_up_returns_none():
    strategy = _make_strategy("atm_sell_macd_zero", _config("SELL"), [24650.0])
    engine = _FakeMacdEngine(signal="BULLISH")
    engine.is_warmed_up = False
    strategy.tick_macd = engine
    assert strategy.on_tick_price(24650.0) is None


def test_exit_on_fixed_sl():
    strategy = _make_strategy("atm_sell_macd_zero", _config("SELL"), [24650.0])
    strategy.tick_macd = _FakeMacdEngine(signal="BULLISH")
    intent = strategy.on_tick_price(24650.0)
    from angel_auto.oms.order_manager import OrderManager
    from angel_auto.broker.paper_broker import PaperBroker

    broker = PaperBroker(strategy.option_chain, starting_capital_rs=100000.0)
    oms = OrderManager(broker)
    oms.execute_entry(intent, strategy_name="atm_sell_macd_zero")

    # SL leg is a short PUT entered near ltp=100 - move price up sharply against the short
    leg = journal.get_open_position(strategy_name="atm_sell_macd_zero")["legs"][0]
    strategy.option_chain.update_ltp(leg["token"], 200.0)  # short PE: big adverse move

    strategy.tick_macd = _FakeMacdEngine(signal="NONE", side="BULLISH")
    exit_intent = strategy.on_tick_price(24650.0)
    assert isinstance(exit_intent, ExitIntent)
    assert exit_intent.reason == ExitReason.FIXED_SL


def test_exit_on_opposite_signal_state():
    strategy = _make_strategy("atm_sell_macd_zero", _config("SELL"), [24650.0])
    strategy.tick_macd = _FakeMacdEngine(signal="BULLISH", side="BULLISH")
    intent = strategy.on_tick_price(24650.0)
    from angel_auto.oms.order_manager import OrderManager
    from angel_auto.broker.paper_broker import PaperBroker

    broker = PaperBroker(strategy.option_chain, starting_capital_rs=100000.0)
    oms = OrderManager(broker)
    oms.execute_entry(intent, strategy_name="atm_sell_macd_zero")

    # direction was LONG (bullish entry); flip the engine's current_side to BEARISH (opposite)
    strategy.tick_macd = _FakeMacdEngine(signal="NONE", side="BEARISH")
    exit_intent = strategy.on_tick_price(24650.0)
    assert isinstance(exit_intent, ExitIntent)
    assert exit_intent.reason == ExitReason.OPPOSITE_ZERO_CROSS


def test_manual_exit_and_square_off_scoped_to_this_strategy():
    strategy = _make_strategy("atm_sell_macd_zero", _config("SELL"), [24650.0])
    assert strategy.manual_exit() is None
    assert strategy.on_square_off_trigger() is None

    strategy.tick_macd = _FakeMacdEngine(signal="BULLISH")
    intent = strategy.on_tick_price(24650.0)
    from angel_auto.oms.order_manager import OrderManager
    from angel_auto.broker.paper_broker import PaperBroker

    broker = PaperBroker(strategy.option_chain, starting_capital_rs=100000.0)
    oms = OrderManager(broker)
    oms.execute_entry(intent, strategy_name="atm_sell_macd_zero")

    assert strategy.manual_exit().reason == ExitReason.MANUAL_EXIT

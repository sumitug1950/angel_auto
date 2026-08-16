import time
from datetime import datetime, timedelta

from angel_auto.analytics.black_scholes import bs_price
from angel_auto.data.instruments import Instrument, InstrumentMaster
from angel_auto.data.live_feed import GreeksRefresher, LiveFeedRouter, build_subscription_tokens
from angel_auto.data.market_data import BarAggregator, OptionChainSnapshot


def test_router_feeds_spot_ticks_to_bar_aggregator():
    bars = BarAggregator(interval_sec=15)
    chain = OptionChainSnapshot()
    router = LiveFeedRouter(spot_token="26000", bar_aggregator=bars, option_chain=chain)

    router.on_tick({"token": "26000", "last_traded_price": 2480000})  # paise -> 24800.00
    assert router.latest_spot == 24800.0
    assert bars.candle_count == 1


def test_router_feeds_option_ticks_to_option_chain():
    bars = BarAggregator(interval_sec=15)
    chain = OptionChainSnapshot()
    chain.register("45116", "NIFTY18AUG2624600CE", 24600.0, "CE")
    router = LiveFeedRouter(spot_token="26000", bar_aggregator=bars, option_chain=chain)

    router.on_tick({"token": "45116", "last_traded_price": 30000})  # -> 300.00
    assert chain.get("45116").ltp == 300.0
    assert bars.candle_count == 0  # not a spot tick


def test_router_ignores_missing_or_zero_price():
    bars = BarAggregator(interval_sec=15)
    chain = OptionChainSnapshot()
    router = LiveFeedRouter(spot_token="26000", bar_aggregator=bars, option_chain=chain)

    router.on_tick({"token": "26000"})  # no price key
    router.on_tick({"token": "26000", "last_traded_price": 0})
    assert router.latest_spot == 0.0
    assert bars.candle_count == 0


def _fake_instruments_with_ce_pe(expiry: str, strikes: list[float]) -> InstrumentMaster:
    master = InstrumentMaster()
    instruments = []
    token = 1
    for strike in strikes:
        for opt_type in ("CE", "PE"):
            instruments.append(
                Instrument(
                    token=str(token), symbol=f"NIFTY{expiry}{int(strike)}{opt_type}", name="NIFTY",
                    expiry=expiry, strike=strike, lot_size=65, instrument_type="OPTIDX", exchange="NFO",
                )
            )
            token += 1
    master._by_name_type_expiry = {("NIFTY", "OPTIDX", expiry): instruments}
    master._loaded = True
    return master


def test_build_subscription_tokens_filters_by_grid_and_band():
    expiry = "29SEP2026"
    strikes = [24300.0, 24350.0, 24400.0, 24500.0, 24600.0, 24700.0, 25200.0]  # 24350 off-grid
    instruments = _fake_instruments_with_ce_pe(expiry, strikes)
    chain = OptionChainSnapshot()

    tokens = build_subscription_tokens(
        instruments, chain, "NIFTY", expiry, center_strike=24600.0, band_points=300.0, grid=100.0
    )

    registered_strikes = {q.strike for q in chain.all_quotes()}
    assert 24350.0 not in registered_strikes  # off-grid
    assert 25200.0 not in registered_strikes  # outside band
    assert 24300.0 in registered_strikes
    assert 24700.0 in registered_strikes
    # 2 option types (CE+PE) per in-range on-grid strike: 24300,24400,24500,24600,24700 = 5 strikes
    assert len(tokens) == 10


def test_greeks_refresher_time_to_expiry_positive_for_future_expiry():
    future_expiry = (datetime.now() + timedelta(days=10)).strftime("%d%b%Y").upper()
    years = GreeksRefresher._time_to_expiry_years(future_expiry)
    assert 0 < years < (15 / 365)  # roughly 10-11 days


def test_greeks_refresher_time_to_expiry_zero_for_past_expiry():
    past_expiry = (datetime.now() - timedelta(days=5)).strftime("%d%b%Y").upper()
    years = GreeksRefresher._time_to_expiry_years(past_expiry)
    assert years == 0.0


def test_greeks_refresher_updates_delta_on_manual_cycle():
    chain = OptionChainSnapshot()
    chain.register("1", "NIFTY_TEST_CE", 24600.0, "CE")
    spot, strike, t, rate, iv = 24800.0, 24600.0, 10 / 365, 0.065, 0.14
    price = bs_price(spot, strike, t, rate, iv, "CE")
    chain.update_ltp("1", price)

    expiry = (datetime.now() + timedelta(days=10)).strftime("%d%b%Y").upper()
    refresher = GreeksRefresher(chain, get_spot=lambda: spot, get_expiry=lambda: expiry, rate=rate)
    refresher._refresh_once()

    quote = chain.get("1")
    assert quote.delta is not None
    assert quote.delta > 0.5  # ITM call


def test_greeks_refresher_start_stop_lifecycle():
    chain = OptionChainSnapshot()
    expiry = (datetime.now() + timedelta(days=10)).strftime("%d%b%Y").upper()
    refresher = GreeksRefresher(chain, get_spot=lambda: 0.0, get_expiry=lambda: expiry, rate=0.065, interval_sec=0.05)
    refresher.start()
    time.sleep(0.15)
    refresher.stop()  # should not raise, thread should be joined

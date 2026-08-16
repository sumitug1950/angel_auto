from angel_auto.analytics.charges import estimate_charges_rs
from angel_auto.core.enums import OrderSide
from angel_auto.settings import ChargesConfig

CFG = ChargesConfig(
    brokerage_per_order_rs=40.0,
    stt_sell_pct=0.1,
    exchange_txn_pct=0.03503,
    gst_pct=18.0,
    sebi_charges_pct=0.0001,
    stamp_duty_buy_pct=0.003,
)


def test_brokerage_is_flat_per_order_times_two():
    result = estimate_charges_rs(entry_price=100.0, exit_price=110.0, quantity=65, entry_side=OrderSide.BUY, config=CFG)
    assert result.brokerage_rs == 80.0  # Rs 40 x 2 (entry + exit orders), independent of price/quantity


def test_stt_charged_only_on_the_sell_leg_when_entry_is_sell():
    # ATM-sell strategy: entry SELL (turnover = entry_price*qty), exit BUY
    result = estimate_charges_rs(entry_price=100.0, exit_price=80.0, quantity=65, entry_side=OrderSide.SELL, config=CFG)
    expected_stt = (100.0 * 65) * (0.1 / 100.0)
    assert result.stt_rs == round(expected_stt, 2)


def test_stt_charged_only_on_the_sell_leg_when_exit_is_sell():
    # ITM4-buy strategy: entry BUY, exit SELL (turnover = exit_price*qty)
    result = estimate_charges_rs(entry_price=100.0, exit_price=120.0, quantity=65, entry_side=OrderSide.BUY, config=CFG)
    expected_stt = (120.0 * 65) * (0.1 / 100.0)
    assert result.stt_rs == round(expected_stt, 2)


def test_stamp_duty_charged_only_on_the_buy_leg():
    # entry_side=SELL -> buy leg is the exit
    result = estimate_charges_rs(entry_price=100.0, exit_price=90.0, quantity=65, entry_side=OrderSide.SELL, config=CFG)
    expected_stamp_duty = (90.0 * 65) * (0.003 / 100.0)
    assert result.stamp_duty_rs == round(expected_stamp_duty, 2)


def test_gst_applies_only_to_brokerage_and_exchange_charges():
    result = estimate_charges_rs(entry_price=100.0, exit_price=110.0, quantity=65, entry_side=OrderSide.BUY, config=CFG)
    expected_gst = (result.brokerage_rs + result.exchange_txn_rs) * (18.0 / 100.0)
    assert result.gst_rs == round(expected_gst, 2)


def test_total_is_sum_of_all_components():
    result = estimate_charges_rs(entry_price=100.0, exit_price=110.0, quantity=65, entry_side=OrderSide.BUY, config=CFG)
    expected = (
        result.brokerage_rs + result.stt_rs + result.exchange_txn_rs + result.sebi_charges_rs
        + result.stamp_duty_rs + result.gst_rs
    )
    assert result.total_rs == round(expected, 2)


def test_zero_price_and_quantity_do_not_crash():
    result = estimate_charges_rs(entry_price=0.0, exit_price=0.0, quantity=0, entry_side=OrderSide.BUY, config=CFG)
    assert result.stt_rs == 0.0
    assert result.exchange_txn_rs == 0.0
    assert result.brokerage_rs == 80.0  # flat regardless of price/quantity

"""Estimated Indian F&O brokerage + statutory charges for one closed option leg (entry +
exit round trip). Illustrative approximations of typical discount-broker charges - NOT a
guaranteed match to any specific broker's exact current tariff. Rates are configurable
(settings.ChargesConfig / config.yaml's charges: block) precisely so they can be corrected
without a code change - cross-check against a real contract note before trusting the
net-P&L figures this produces for any real decision.
"""
from __future__ import annotations

from dataclasses import dataclass

from angel_auto.core.enums import OrderSide
from angel_auto.settings import ChargesConfig


@dataclass
class ChargesBreakdown:
    brokerage_rs: float
    stt_rs: float
    exchange_txn_rs: float
    sebi_charges_rs: float
    stamp_duty_rs: float
    gst_rs: float
    total_rs: float


def estimate_charges_rs(
    entry_price: float, exit_price: float, quantity: int, entry_side: OrderSide, config: ChargesConfig
) -> ChargesBreakdown:
    """One leg's full round-trip charges. `entry_side` is the side of the entry order
    (BUY for the ITM4-buy strategy, SELL for the ATM-sell strategy) - the exit order is
    always the opposite side."""
    entry_turnover = entry_price * quantity
    exit_turnover = exit_price * quantity
    sell_turnover = entry_turnover if entry_side == OrderSide.SELL else exit_turnover
    buy_turnover = exit_turnover if entry_side == OrderSide.SELL else entry_turnover

    brokerage = config.brokerage_per_order_rs * 2  # one order to enter, one to exit
    stt = sell_turnover * (config.stt_sell_pct / 100.0)
    exchange_txn = (entry_turnover + exit_turnover) * (config.exchange_txn_pct / 100.0)
    sebi = (entry_turnover + exit_turnover) * (config.sebi_charges_pct / 100.0)
    stamp_duty = buy_turnover * (config.stamp_duty_buy_pct / 100.0)
    gst = (brokerage + exchange_txn) * (config.gst_pct / 100.0)  # GST applies to brokerage + exchange charges only
    total = brokerage + stt + exchange_txn + sebi + stamp_duty + gst

    return ChargesBreakdown(
        brokerage_rs=round(brokerage, 2),
        stt_rs=round(stt, 2),
        exchange_txn_rs=round(exchange_txn, 2),
        sebi_charges_rs=round(sebi, 2),
        stamp_duty_rs=round(stamp_duty, 2),
        gst_rs=round(gst, 2),
        total_rs=round(total, 2),
    )

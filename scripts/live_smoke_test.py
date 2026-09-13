"""LIVE smoke test - real orders on your real Angel One account. Run it during market hours,
with the Angel One app open next to it, when you want proof that the order paths the trading
app depends on work end to end.

  1. BUY 1 lot of a cheap far out-of-the-money weekly CALL (Rs 1-5 premium) with a
     marketable LIMIT - the same path the app's entries use - and confirm the fill.
  2. Place a STOPLOSS_LIMIT SELL far below it (the broker backup-SL order type) and cancel it
     again the way an app exit does.
  3. SELL it back with a MARKET order - the app's exit path - and confirm the fill.
  4. Confirm the position is flat at the broker.

Typical cost: the option's bid/ask spread on the lot + about Rs 80-100 of brokerage and taxes.
Runs under its own strategy name, so the flagship's daily trade count and P&L are untouched.

Usage (09:15-15:30 only):
    .venv\\Scripts\\python.exe scripts\\live_smoke_test.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from angel_auto.analytics.charges import estimate_charges_rs
from angel_auto.broker.angelone_auth import login, logout
from angel_auto.broker.angelone_rest import SL_VARIETY, AngelOneBroker
from angel_auto.broker.base import OrderRequest
from angel_auto.core.enums import Direction, ExitReason, OptionType, OrderSide, OrderStatus, PositionStatus, StructureType
from angel_auto.data.instruments import InstrumentMaster
from angel_auto.logging_conf import configure_logging
from angel_auto.oms.order_manager import OrderManager, OrderStuckError, order_tag, to_tick
from angel_auto.persistence import journal
from angel_auto.persistence.db import init_db
from angel_auto.scheduler.jobs import is_market_open, parse_hhmm
from angel_auto.settings import get_settings
from angel_auto.strategy.base import LegIntent

STRATEGY_NAME = "live_smoke_test"
PREMIUM_RANGE_RS = (1.0, 5.0)


def step(title: str) -> None:
    print(f"\n=== {title}")


def main() -> int:
    configure_logging()
    settings = get_settings()
    app_cfg = settings.app
    now = datetime.now(ZoneInfo(app_cfg.timezone))
    if not is_market_open(now, parse_hhmm(app_cfg.market_hours.open), parse_hhmm(app_cfg.market_hours.close)):
        print("Market band hai - ye test sirf market time (09:15-15:30) mein chalta hai.")
        return 1

    print("=" * 70)
    print("  LIVE SMOKE TEST - ASLI ORDER JAAYENGE (kharcha lagbhag Rs 100)")
    print("  Angel One app khuli rakhein. WARP/VPN band hona chahiye.")
    print("=" * 70)
    if input("Chalana hai? Haan ho to YES likhein: ").strip() != "YES":
        print("Cancel kar diya.")
        return 1

    init_db()
    session = login(settings.credentials)
    broker = AngelOneBroker(session)
    oms = OrderManager(
        broker,
        product_type=app_cfg.oms.product_type,
        entry_slippage_buffer_pts=app_cfg.oms.entry_slippage_buffer_pts,
        fill_timeout_sec=app_cfg.oms.fill_timeout_sec,
        fill_poll_interval_sec=app_cfg.oms.fill_poll_interval_sec,
        exit_attempts=app_cfg.oms.exit_attempts,
        charges_config=app_cfg.charges,
    )
    pick = None
    holding = False
    try:
        instruments = InstrumentMaster()
        instruments.load()
        expiry = instruments.nearest_weekly_expiry(app_cfg.underlying)
        spot_token = instruments.nifty_spot_historical_instrument().token
        quote = session.smart_connect.getMarketData("LTP", {"NSE": [spot_token]}) or {}
        spot = float(((quote.get("data") or {}).get("fetched") or [{}])[0].get("ltp") or 0)
        if spot <= 0:
            print("Nifty ka live price nahi mila - test nahi chalaya.")
            return 1

        candidates = sorted(
            (i for i in instruments.option_chain(app_cfg.underlying, expiry) if i.symbol.endswith("CE") and i.strike > spot + 300),
            key=lambda i: i.strike,
        )[:50]
        quotes = session.smart_connect.getMarketData("LTP", {"NFO": [i.token for i in candidates]}) or {}
        prices = {str(r["symbolToken"]): float(r["ltp"]) for r in ((quotes.get("data") or {}).get("fetched") or [])}
        pick = next((i for i in candidates if PREMIUM_RANGE_RS[0] <= prices.get(i.token, 0) <= PREMIUM_RANGE_RS[1]), None)
        if pick is None:
            print(f"Rs {PREMIUM_RANGE_RS[0]:g}-{PREMIUM_RANGE_RS[1]:g} wala CALL nahi mila - test nahi chalaya.")
            return 1
        qty = pick.lot_size
        print(f"Nifty {spot:.2f} | option {pick.symbol} | LTP Rs {prices[pick.token]} | qty {qty}")

        position_id = journal.create_position(Direction.LONG, StructureType.DEBIT, expiry, strategy_name=STRATEGY_NAME)
        leg_id = journal.add_leg(position_id, pick.token, pick.symbol, OptionType.CE, pick.strike, "ITM", OrderSide.BUY, qty)
        leg = LegIntent(OptionType.CE, pick.strike, "ITM", OrderSide.BUY, pick.token, pick.symbol, qty)

        step("1. BUY - marketable LIMIT (app ki entry jaisa)")
        entry_price = oms._enter_leg(leg_id, leg)
        if entry_price is None:
            print(f"FAIL: fill nahi hua - {oms._leg_message}")
            journal.update_position_status(position_id, PositionStatus.ABORTED)
            return 1
        holding = True
        journal.update_leg_fill(leg_id, entry_price=entry_price)
        journal.update_position_status(position_id, PositionStatus.OPEN, set_entry_time=True)
        held = {p.token: p for p in broker.get_positions()}
        print(f"PASS: fill @ Rs {entry_price} | Angel One position: {held.get(pick.token)}")

        step("2. STOPLOSS_LIMIT SELL (broker backup SL jaisa) - lagana aur cancel karna")
        trigger = to_tick(max(entry_price * 0.2, 0.10), up=False)
        limit_price = to_tick(max(trigger - 0.05, 0.05), up=False)
        sl_order_id = journal.add_order(leg_id, OrderSide.SELL, "SL", qty, price=limit_price, trigger_price=trigger, is_safety_net=True)
        result = broker.place_order(OrderRequest(
            exchange="NFO", trading_symbol=pick.symbol, token=pick.token, side=OrderSide.SELL, quantity=qty,
            order_type="SL", product_type=app_cfg.oms.product_type, price=limit_price, trigger_price=trigger,
            variety=SL_VARIETY, tag=order_tag(sl_order_id),
        ))
        if result.status != OrderStatus.OPEN or not result.broker_order_id:
            print(f"FAIL: SL order nahi laga - {result.message}")
            journal.update_order_status(sl_order_id, OrderStatus.REJECTED, reject_reason=result.message)
        else:
            journal.update_order_status(sl_order_id, OrderStatus.OPEN, broker_order_id=result.broker_order_id)
            print(f"SL order laga: trigger Rs {trigger} limit Rs {limit_price} -> {broker.get_order_state(result.broker_order_id)}")
            oms._stand_down_backup_sls({"id": position_id})
            state = broker.get_order_state(result.broker_order_id)
            print(("PASS" if state.status == OrderStatus.CANCELLED else "FAIL") + f": SL cancel ke baad status {state.status.value}")

        step("3. SELL - MARKET (app ke exit jaisa)")
        closed, exit_price = oms._close_quantity(leg_id, pick.symbol, pick.token, OrderSide.BUY, qty)
        if closed < qty:
            print(f"FAIL: sirf {closed}/{qty} bika - {oms._leg_message}. ANGEL ONE APP SE BACHI QTY KHUD BECHO.")
            return 1
        holding = False
        journal.update_leg_fill(leg_id, exit_price=exit_price)
        gross = (exit_price - entry_price) * qty
        charges = estimate_charges_rs(entry_price, exit_price, qty, OrderSide.BUY, app_cfg.charges).total_rs
        journal.close_position(position_id, ExitReason.MANUAL_EXIT, gross, strategy_name=STRATEGY_NAME, charges_rs=charges)
        print(f"PASS: exit @ Rs {exit_price} | gross Rs {gross:.2f} | charges ~Rs {charges:.2f} | net ~Rs {gross - charges:.2f}")

        step("4. Pakka karna: Angel One par position band")
        still = [p for p in broker.get_positions() if p.token == pick.token]
        print("PASS: position flat" if not still else f"FAIL: abhi bhi khuli: {still}")
        return 0 if not still else 1
    except OrderStuckError as exc:
        print(f"\nFAIL: {exc}")
        return 1
    finally:
        if holding and pick is not None:
            print(f"\n!!! {pick.symbol} ABHI BHI KHULA HO SAKTA HAI - ANGEL ONE APP MEIN CHECK KARKE KHUD BECHEIN !!!")
        logout(session)


if __name__ == "__main__":
    sys.exit(main())

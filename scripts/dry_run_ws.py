"""Standalone check: connect to Angel One WebSocket and print live Nifty spot ticks.

Usage:
    .venv\\Scripts\\python.exe scripts\\dry_run_ws.py [seconds_to_run]

Subscribes read-only to the NIFTY spot index feed - no orders placed. Prints each tick's
LTP as it arrives, then disconnects cleanly after the given duration (default 30s).
"""
from __future__ import annotations

import sys
import time
from datetime import datetime

from angel_auto.broker.angelone_auth import AngelOneAuthError, login, logout
from angel_auto.broker.angelone_ws import EXCHANGE_NSE_CM, MODE_LTP, AngelOneWebSocket
from angel_auto.data.instruments import InstrumentMaster
from angel_auto.logging_conf import configure_logging, get_logger
from angel_auto.settings import get_settings

log = get_logger("dry_run_ws")

tick_count = 0


def on_tick(tick: dict) -> None:
    global tick_count
    tick_count += 1
    ltp_rupees = tick.get("last_traded_price", 0) / 100.0
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] tick #{tick_count}  token={tick.get('token')}  LTP={ltp_rupees:.2f}")


def main() -> int:
    configure_logging()
    settings = get_settings()
    run_seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

    print("Logging in...")
    try:
        session = login(settings.credentials)
    except AngelOneAuthError as exc:
        print(f"LOGIN FAILED: {exc}")
        return 1
    print("Login OK.")

    print("Loading instrument master (NIFTY spot token)...")
    instruments = InstrumentMaster()
    instruments.load()
    spot = instruments.nifty_spot_instrument()
    print(f"NIFTY spot: token={spot.token} exchange={spot.exchange}")

    print("Connecting WebSocket...")
    ws = AngelOneWebSocket(session, on_tick=on_tick)
    try:
        ws.start(timeout_sec=15)
    except Exception as exc:
        print(f"WEBSOCKET CONNECT FAILED: {exc}")
        logout(session)
        return 1
    print("WebSocket connected.")

    ws.subscribe(EXCHANGE_NSE_CM, [spot.token], mode=MODE_LTP)
    print(f"Subscribed to NIFTY spot. Streaming for {run_seconds:.0f}s...")

    time.sleep(run_seconds)

    print(f"Done. Received {tick_count} tick(s).")
    ws.close()
    logout(session)

    if tick_count == 0:
        print("WARNING: zero ticks received - market may be closed right now, or check subscription.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

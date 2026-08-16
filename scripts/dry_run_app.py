"""Standalone check: start the full TradingApp (real login + real WebSocket, paper broker
for orders) and run for a short window, then stop cleanly. Proves the composition root
actually wires broker -> live feed -> strategy -> OMS -> scheduler together, not just that
each piece works in isolation.

Places no real orders (paper mode only). Safe to run even when the market is closed - the
WS connection and job scheduling still get exercised, just no ticks/candles will build up.

Usage:
    .venv\\Scripts\\python.exe scripts\\dry_run_app.py [seconds_to_run]
"""
from __future__ import annotations

import sys
import time

from angel_auto.core.app import TradingApp
from angel_auto.logging_conf import configure_logging
from angel_auto.settings import get_settings


def main() -> int:
    configure_logging()
    settings = get_settings()
    run_seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

    print(f"Starting TradingApp (mode={settings.app.mode.value}) for {run_seconds:.0f}s...")
    app = TradingApp(settings)
    try:
        app.start()
    except Exception as exc:
        print(f"APP START FAILED: {exc}")
        return 1

    print("App started. Live status:")
    print(f"  latest spot: {app._router.latest_spot}")
    print(f"  latest vix: {app._router.latest_vix}")
    print(f"  candles so far: {app.bars.candle_count}")
    print(f"  option quotes subscribed: {len(app.option_chain.all_quotes())}")
    print(f"  scheduler jobs: {[j.id for j in app._scheduler.scheduler.get_jobs()]}")

    time.sleep(run_seconds)

    print("Stopping...")
    app.stop()
    print("Stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

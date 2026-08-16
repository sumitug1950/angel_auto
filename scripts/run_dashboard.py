"""Runs the dashboard - the main entry point for actually operating the system.

On startup this logs in to Angel One for real, connects the live WebSocket feed, and
starts the strategy/OMS/scheduler loop (paper mode only right now). Ctrl+C stops it
cleanly (broker logout, WS close, scheduler shutdown).

Usage:
    .venv\\Scripts\\python.exe scripts\\run_dashboard.py
"""
from __future__ import annotations

import uvicorn

from angel_auto.settings import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "angel_auto.dashboard.main:app",
        host=settings.app.dashboard.host,
        port=settings.app.dashboard.port,
        reload=False,
    )

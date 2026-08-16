"""Shared dashboard state - the one running TradingApp instance every route reads/writes
through. A separate module (not main.py) purely to avoid a main<->routes circular import.
"""
from __future__ import annotations

from angel_auto.core.app import TradingApp

app_state: dict[str, TradingApp | None] = {"trading_app": None}


def get_trading_app() -> TradingApp:
    trading_app = app_state["trading_app"]
    if trading_app is None:
        raise RuntimeError("TradingApp not started yet")
    return trading_app

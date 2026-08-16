"""Shared dashboard state - the one running TradingApp instance every route reads/writes
through. A separate module (not main.py) purely to avoid a main<->routes circular import.
"""
from __future__ import annotations

import threading
from collections import deque

from angel_auto.core.app import TradingApp

app_state: dict[str, TradingApp | None] = {"trading_app": None}


def get_trading_app() -> TradingApp:
    trading_app = app_state["trading_app"]
    if trading_app is None:
        raise RuntimeError("TradingApp not started yet")
    return trading_app


class TickBroadcaster:
    """Thread-safe pub/sub bridge between the sync tick-callback thread (LiveFeedRouter,
    running on the WebSocket client's own thread) and the dashboard's async /ws/ticks
    handler. The WS handler polls `since()` on a short interval rather than using an
    asyncio.Queue directly, since asyncio.Queue isn't safe to write to from another OS
    thread without extra call_soon_threadsafe plumbing - a small poll interval is simpler
    and robust enough for a live chart."""

    def __init__(self, maxlen: int = 4000) -> None:
        self._buffer: deque[tuple[int, dict]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def publish(self, message: dict) -> None:
        with self._lock:
            self._seq += 1
            self._buffer.append((self._seq, message))

    def since(self, last_seq: int) -> tuple[list[dict], int]:
        with self._lock:
            items = [(seq, msg) for seq, msg in self._buffer if seq > last_seq]
        if not items:
            return [], last_seq
        return [msg for _, msg in items], items[-1][0]


tick_broadcaster = TickBroadcaster()

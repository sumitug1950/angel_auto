"""Live tick archive - records every spot/VIX/option tick to the DB (TickRecord table) so
a future replay-backtest can use real captured prices, since Angel One doesn't reliably
provide historical intraday/tick data. `.record()` only appends to an in-memory buffer (no
DB I/O on the tick path); a background thread flushes it in batches, same start/stop
lifecycle pattern as data/live_feed.py's GreeksRefresher.

Known limitation: grows a few MB/day at Nifty tick volumes - pruning/archiving old rows is
a future follow-up, not handled here.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal

log = get_logger(__name__)


class TickRecorder:
    def __init__(self, flush_interval_sec: float = 2.0) -> None:
        self.flush_interval_sec = flush_interval_sec
        self._buffer: list[dict] = []
        self._buffer_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def record(self, token: str, tick_type: str, trading_symbol: str | None, ltp: float) -> None:
        row = {
            "token": token,
            "tick_type": tick_type,
            "trading_symbol": trading_symbol,
            "ltp": ltp,
            "recorded_at": datetime.now(timezone.utc),
        }
        with self._buffer_lock:
            self._buffer.append(row)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tick-recorder", daemon=True)
        self._thread.start()
        log.info("tick_recorder_started", flush_interval_sec=self.flush_interval_sec)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._flush()
        log.info("tick_recorder_stopped")

    def _run(self) -> None:
        while not self._stop_event.wait(self.flush_interval_sec):
            self._flush()

    def _flush(self) -> None:
        with self._buffer_lock:
            if not self._buffer:
                return
            rows, self._buffer = self._buffer, []
        try:
            journal.bulk_insert_ticks(rows)
        except Exception:
            log.exception("tick_recorder_flush_failed")

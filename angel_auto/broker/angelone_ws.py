"""Angel One WebSocket v2 wrapper - live tick streaming.

SmartWebSocketV2.connect() blocks the calling thread (it runs the websocket event loop
via run_forever()), so we run it in a background thread here and expose a plain callback
for ticks. The SDK already retries/reconnects internally on error (configurable below);
this wrapper adds the connect-with-timeout and a typed tick callback on top.

Tick dicts handed to on_tick have (at minimum) "token", "last_traded_price" (integer
paise - divide by 100 for rupees), and "exchange_timestamp" (epoch ms).
"""
from __future__ import annotations

import threading
import types
from collections.abc import Callable
from typing import Any

from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from angel_auto.broker.angelone_auth import AngelSession
from angel_auto.logging_conf import get_logger

log = get_logger(__name__)

TickCallback = Callable[[dict[str, Any]], None]

# exchangeType values per SmartWebSocketV2.subscribe()
EXCHANGE_NSE_CM = 1
EXCHANGE_NSE_FO = 2

# subscription modes
MODE_LTP = SmartWebSocketV2.LTP_MODE
MODE_QUOTE = SmartWebSocketV2.QUOTE
MODE_SNAP_QUOTE = SmartWebSocketV2.SNAP_QUOTE


class AngelOneWebSocketError(RuntimeError):
    pass


class AngelOneWebSocket:
    """One WS connection, N token subscriptions, one tick callback."""

    def __init__(
        self,
        session: AngelSession,
        on_tick: TickCallback,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        max_retry_attempt: int = 5,
    ) -> None:
        self._on_tick = on_tick
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

        self._ws = SmartWebSocketV2(
            auth_token=session.jwt_token,
            api_key=session.smart_connect.api_key,
            client_code=session.client_code,
            feed_token=session.feed_token,
            max_retry_attempt=max_retry_attempt,
            retry_strategy=1,  # exponential backoff
        )
        self._ws.on_open = self._handle_open
        self._ws.on_data = self._handle_data
        self._ws.on_error = self._handle_error
        self._ws.on_close = self._handle_close
        # smartapi-python 1.5.5's SmartWebSocketV2._on_close(self, wsapp) doesn't accept the
        # (close_status_code, close_msg) args every current websocket-client version passes to
        # on_close - that raises inside the SDK's own callback dispatcher, which in turn trips
        # its on_error path and fires a spurious reconnect attempt even on an intentional close.
        # Patch the instance method around it rather than touching site-packages.
        self._ws._on_close = types.MethodType(self._patched_on_close, self._ws)

        self._thread: threading.Thread | None = None
        self._connected_event = threading.Event()

    def start(self, timeout_sec: float = 15.0) -> None:
        """Connect in a background thread; blocks the caller until connected or timeout."""
        self._thread = threading.Thread(target=self._ws.connect, name="angelone-ws", daemon=True)
        self._thread.start()
        if not self._connected_event.wait(timeout=timeout_sec):
            raise AngelOneWebSocketError("Angel One WebSocket did not connect within timeout")

    def subscribe(self, exchange_type: int, tokens: list[str], mode: int = MODE_QUOTE) -> None:
        self._ws.subscribe("angelauto01", mode, [{"exchangeType": exchange_type, "tokens": tokens}])
        log.info("angelone_ws_subscribed", exchange_type=exchange_type, tokens=tokens, mode=mode)

    def unsubscribe(self, exchange_type: int, tokens: list[str], mode: int = MODE_QUOTE) -> None:
        self._ws.unsubscribe("angelauto01", mode, [{"exchangeType": exchange_type, "tokens": tokens}])

    def close(self) -> None:
        self._ws.close_connection()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("angelone_ws_closed_by_caller")

    @property
    def is_connected(self) -> bool:
        return self._connected_event.is_set()

    def _handle_open(self, wsapp: Any) -> None:
        log.info("angelone_ws_connected")
        self._connected_event.set()
        if self._on_connect:
            self._on_connect()

    def _handle_data(self, wsapp: Any, message: dict) -> None:
        try:
            self._on_tick(message)
        except Exception:
            log.exception("angelone_ws_tick_callback_error")

    def _handle_error(self, wsapp: Any, error: Any) -> None:
        log.error("angelone_ws_error", error=str(error))

    def _handle_close(self, wsapp: Any) -> None:
        log.warning("angelone_ws_closed")
        self._connected_event.clear()
        if self._on_disconnect:
            self._on_disconnect()

    @staticmethod
    def _patched_on_close(sdk_self: Any, wsapp: Any, close_status_code: Any = None, close_msg: Any = None) -> None:
        """Replacement for SmartWebSocketV2._on_close - see comment in __init__."""
        if sdk_self.on_close:
            sdk_self.on_close(wsapp)

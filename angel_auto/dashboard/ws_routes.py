"""WebSocket push - the browser gets live status (spot, VIX, position, P&L) and live chart
updates without polling. Status reuses the exact payload GET /api/status serves."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from angel_auto.dashboard.state import get_trading_app, tick_broadcaster
from angel_auto.dashboard.status import build_status_payload
from angel_auto.logging_conf import get_logger

log = get_logger(__name__)
router = APIRouter()

PUSH_INTERVAL_SEC = 1.5
TICK_POLL_INTERVAL_SEC = 0.1


@router.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            try:
                payload = build_status_payload(get_trading_app())
                await websocket.send_json(jsonable_encoder(payload))
            except RuntimeError:
                await websocket.send_json({"error": "TradingApp not started yet"})
            await asyncio.sleep(PUSH_INTERVAL_SEC)
    except WebSocketDisconnect:
        log.info("dashboard_ws_client_disconnected")


@router.websocket("/ws/ticks")
async def ws_ticks(websocket: WebSocket) -> None:
    """Live candle + MACD updates for the chart. Starts from *now*: history comes from
    GET /api/chart, so replaying the broadcaster's backlog would only resend older bars."""
    await websocket.accept()
    last_seq = tick_broadcaster.latest_seq()
    try:
        while True:
            messages, last_seq = tick_broadcaster.since(last_seq)
            for message in messages:
                await websocket.send_json(message)
            await asyncio.sleep(TICK_POLL_INTERVAL_SEC)
    except WebSocketDisconnect:
        log.info("dashboard_ws_ticks_client_disconnected")

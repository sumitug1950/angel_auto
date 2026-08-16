"""WebSocket push - the browser gets live status (spot, VIX, position, P&L) without polling.
Reuses the same status payload as GET /api/status so the two never drift apart."""
from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder

from angel_auto.dashboard.state import get_trading_app, tick_broadcaster
from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal
from angel_auto.scheduler.jobs import is_market_open, parse_hhmm

log = get_logger(__name__)
router = APIRouter()

PUSH_INTERVAL_SEC = 1.5
TICK_POLL_INTERVAL_SEC = 0.1


def _zero_cross_status(app) -> dict:
    result = {}
    for name, strategy in app.zero_cross_strategies.items():
        open_position = journal.get_open_position(strategy_name=name)
        unrealized_pnl = strategy.position_pnl_rs(open_position) if open_position is not None else 0.0
        daily_state = journal.get_or_create_daily_state(strategy_name=name)
        result[name] = {
            "open_position": open_position,
            "unrealized_pnl_rs": unrealized_pnl,
            "daily_state": daily_state,
            "macd": strategy.tick_macd.macd,
            "signal": strategy.tick_macd.signal,
        }
    return result


def _build_status_payload() -> dict:
    app = get_trading_app()
    daily_state = journal.get_or_create_daily_state()
    open_position = journal.get_open_position()
    pending = journal.get_pending_direction_request()
    unrealized_pnl = app.strategy.position_pnl_rs(open_position) if (app.strategy is not None and open_position is not None) else 0.0
    market_open = is_market_open(
        datetime.now(app._scheduler.tz),
        parse_hhmm(app.settings.app.market_hours.open),
        parse_hhmm(app.settings.app.market_hours.close),
    )
    return {
        "mode": app.settings.app.mode.value,
        "market_open": market_open,
        "spot": app._router.latest_spot,
        "vix": app._router.latest_vix,
        "daily_state": daily_state,
        "open_position": open_position if app.strategy is not None else None,
        "unrealized_pnl_rs": unrealized_pnl,
        "pending_request": pending,
        "structure_preference": journal.get_structure_preference(),
        "zero_cross": _zero_cross_status(app),
    }


@router.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            try:
                payload = _build_status_payload()
                await websocket.send_json(jsonable_encoder(payload))
            except RuntimeError:
                await websocket.send_json({"error": "TradingApp not started yet"})
            await asyncio.sleep(PUSH_INTERVAL_SEC)
    except WebSocketDisconnect:
        log.info("dashboard_ws_client_disconnected")


@router.websocket("/ws/ticks")
async def ws_ticks(websocket: WebSocket) -> None:
    """Live Nifty spot ticks + MACD updates for the dashboard chart - a separate, much
    faster-polled channel from /ws/status (which is built for position/risk state, not a
    price series)."""
    await websocket.accept()
    last_seq = 0
    try:
        while True:
            messages, last_seq = tick_broadcaster.since(last_seq)
            for message in messages:
                await websocket.send_json(message)
            await asyncio.sleep(TICK_POLL_INTERVAL_SEC)
    except WebSocketDisconnect:
        log.info("dashboard_ws_ticks_client_disconnected")

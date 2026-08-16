"""REST API for the dashboard - status, trade log, equity curve, and the manual controls
(Long/Short, cancel-pending, exit-now, kill-switch)."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from angel_auto.core.enums import Direction, StructureType
from angel_auto.dashboard.state import get_trading_app
from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal
from angel_auto.scheduler.jobs import is_market_open, parse_hhmm

log = get_logger(__name__)
router = APIRouter(prefix="/api")


class DirectionRequestBody(BaseModel):
    direction: Direction


class StructureRequestBody(BaseModel):
    structure_type: StructureType


@router.get("/status")
def get_status():
    app = get_trading_app()
    daily_state = journal.get_or_create_daily_state()
    open_position = journal.get_open_position()
    pending = journal.get_pending_direction_request()

    unrealized_pnl = app.strategy.position_pnl_rs(open_position) if open_position is not None else 0.0

    market_open = is_market_open(
        datetime.now(app._scheduler.tz),
        parse_hhmm(app.settings.app.market_hours.open),
        parse_hhmm(app.settings.app.market_hours.close),
    )

    payload = {
        "mode": app.settings.app.mode.value,
        "market_open": market_open,
        "spot": app._router.latest_spot,
        "vix": app._router.latest_vix,
        "daily_state": daily_state,
        "open_position": open_position,
        "unrealized_pnl_rs": unrealized_pnl,
        "pending_request": pending,
        "structure_preference": journal.get_structure_preference(),
    }
    return JSONResponse(content=jsonable_encoder(payload))


@router.get("/trades")
def get_trades(limit: int = 50):
    return JSONResponse(content=jsonable_encoder(journal.list_recent_positions(limit)))


@router.get("/equity-curve")
def get_equity_curve(limit: int = 500):
    return JSONResponse(content=jsonable_encoder(journal.get_equity_curve(limit)))


@router.post("/direction")
def post_direction(body: DirectionRequestBody):
    app = get_trading_app()
    try:
        intent = app.request_direction(body.direction)
    except Exception as exc:
        log.exception("direction_request_api_failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"executed_immediately": intent is not None}


@router.post("/structure")
def post_structure(body: StructureRequestBody):
    app = get_trading_app()
    app.request_structure(body.structure_type)
    return {"structure_preference": body.structure_type.value}


@router.post("/cancel-pending")
def post_cancel_pending():
    app = get_trading_app()
    return {"cancelled": app.cancel_pending()}


@router.post("/exit-now")
def post_exit_now():
    app = get_trading_app()
    app.manual_exit()
    return {"ok": True}


@router.post("/kill-switch")
def post_kill_switch():
    app = get_trading_app()
    app.kill_switch("dashboard kill-switch")
    return {"ok": True}


@router.get("/logs")
def get_logs(lines: int = 150):
    app = get_trading_app()
    log_path = app.settings.app.logging.file
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        return {"lines": all_lines[-lines:]}
    except FileNotFoundError:
        return {"lines": []}

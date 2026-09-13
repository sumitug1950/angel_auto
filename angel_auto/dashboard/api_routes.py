"""REST API for the dashboard - status, chart history, trade log, logs, and the manual
controls (Buying/Selling, Long/Short, cancel-pending, exit-now, kill-switch)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from angel_auto.core.enums import Direction, StructureType
from angel_auto.dashboard.state import get_trading_app
from angel_auto.dashboard.status import build_chart_history, build_status_payload
from angel_auto.logging_conf import get_logger
from angel_auto.persistence import journal

log = get_logger(__name__)
router = APIRouter(prefix="/api")


class DirectionRequestBody(BaseModel):
    direction: Direction


class StructureRequestBody(BaseModel):
    structure_type: StructureType


class ExpiryRequestBody(BaseModel):
    expiry: str


@router.get("/status")
def get_status():
    return JSONResponse(content=jsonable_encoder(build_status_payload(get_trading_app())))


@router.get("/chart")
def get_chart():
    """Every candle since app start + its MACD, so the chart is complete the moment the page
    opens; /ws/ticks then streams the live updates."""
    return JSONResponse(content=build_chart_history(get_trading_app()))


@router.get("/trades")
def get_trades(limit: int = 50, strategy_name: str | None = None):
    return JSONResponse(content=jsonable_encoder(journal.list_recent_positions(limit, strategy_name=strategy_name)))


@router.get("/strategy-summary")
def get_strategy_summary(strategy_name: str):
    return JSONResponse(content=jsonable_encoder(journal.get_strategy_totals(strategy_name)))


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


@router.post("/expiry")
def post_expiry(body: ExpiryRequestBody):
    app = get_trading_app()
    if not app.request_expiry(body.expiry):
        raise HTTPException(status_code=400, detail=f"{body.expiry} abhi chunne layak expiry nahi hai")
    return {"selected_expiry": body.expiry}


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
def get_logs(lines: int = 300):
    app = get_trading_app()
    log_path = app.settings.app.logging.file
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return {"lines": all_lines[-lines:]}
    except FileNotFoundError:
        return {"lines": []}

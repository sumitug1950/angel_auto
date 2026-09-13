"""REST API for the dashboard - status, chart history, trade log, logs, and the manual
controls (expiry, Buying/Selling, Long/Short, Nifty level orders + Nifty SL/target, cancel-pending,
exit-now, kill-switch)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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


class LevelOrderBody(BaseModel):
    direction: Direction
    structure_type: StructureType
    trigger_price: float = Field(gt=0)
    spot_sl: float | None = Field(default=None, gt=0)
    spot_target: float | None = Field(default=None, gt=0)


class LevelOrderChangeBody(BaseModel):
    trigger_price: float = Field(gt=0)
    spot_sl: float | None = Field(default=None, gt=0)
    spot_target: float | None = Field(default=None, gt=0)


class SpotLevelsBody(BaseModel):
    spot_sl: float | None = Field(default=None, gt=0)  # None removes it
    spot_target: float | None = Field(default=None, gt=0)


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


@router.post("/level-order")
def post_level_order(body: LevelOrderBody):
    app = get_trading_app()
    try:
        order_id = app.place_level_order(
            body.direction, body.structure_type, body.trigger_price, spot_sl=body.spot_sl, spot_target=body.spot_target
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"level_order_id": order_id}


@router.post("/level-order/modify")
def post_modify_level_order(body: LevelOrderChangeBody):
    app = get_trading_app()
    try:
        app.modify_level_order(body.trigger_price, spot_sl=body.spot_sl, spot_target=body.spot_target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"trigger_price": body.trigger_price, "spot_sl": body.spot_sl, "spot_target": body.spot_target}


@router.post("/level-order/cancel")
def post_cancel_level_order():
    app = get_trading_app()
    return {"cancelled": app.cancel_level_order()}


@router.post("/position-levels")
def post_position_levels(body: SpotLevelsBody):
    app = get_trading_app()
    try:
        app.set_position_spot_levels(body.spot_sl, body.spot_target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"spot_sl": body.spot_sl, "spot_target": body.spot_target}


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

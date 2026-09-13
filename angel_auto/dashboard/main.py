"""FastAPI dashboard - the main long-running process. Owns the TradingApp lifecycle
(starts it on startup, stops it cleanly on shutdown); every route reads/writes through
that one shared instance (see dashboard/state.py).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from angel_auto.core.app import TradingApp
from angel_auto.dashboard.state import app_state, tick_broadcaster
from angel_auto.dashboard.status import candle_point, macd_points
from angel_auto.logging_conf import configure_logging, get_logger
from angel_auto.settings import get_settings

log = get_logger(__name__)

DASHBOARD_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(DASHBOARD_DIR / "templates"))

MACD_PUBLISH_INTERVAL_SEC = 1.0  # the candle MACD only moves meaningfully once per candle - no need to recompute per tick


def _make_tick_publisher(trading_app: TradingApp) -> Callable[[float], None]:
    """Spot-tick listener for the dashboard chart: the live (still-forming) candle on every
    tick, plus - throttled - the flagship's MACD for that candle, both keyed by the candle's
    start time exactly like GET /api/chart's history."""
    last_macd_publish = 0.0

    def publish(price: float) -> None:
        nonlocal last_macd_publish
        candle = trading_app.bars.last_candle()
        if candle is None:
            return
        tick_broadcaster.publish({"type": "candle", **candle_point(candle)})

        now = time.monotonic()
        if now - last_macd_publish < MACD_PUBLISH_INTERVAL_SEC:
            return
        last_macd_publish = now
        points = macd_points(trading_app)
        if points:
            tick_broadcaster.publish({"type": "macd", **points[-1]})

    return publish


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    configure_logging()
    settings = get_settings()
    trading_app = TradingApp(settings)
    trading_app.start()
    app_state["trading_app"] = trading_app
    if trading_app._router is not None:
        trading_app._router.add_spot_tick_listener(_make_tick_publisher(trading_app))
    log.info("dashboard_startup_complete")
    yield
    trading_app.stop()
    app_state["trading_app"] = None
    log.info("dashboard_shutdown_complete")


def create_app(start_trading_app: bool = True) -> FastAPI:
    """`start_trading_app=False` serves the dashboard over whatever TradingApp is already in
    dashboard.state (tests / offline previews) instead of logging in and starting a real one."""
    fastapi_app = FastAPI(title="angel_auto dashboard", lifespan=lifespan if start_trading_app else None)
    fastapi_app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR / "static")), name="static")

    from angel_auto.dashboard.api_routes import router as api_router
    from angel_auto.dashboard.ws_routes import router as ws_router

    fastapi_app.include_router(api_router)
    fastapi_app.include_router(ws_router)

    @fastapi_app.get("/health")
    def health():
        return {"ok": True}

    @fastapi_app.get("/")
    def index(request: Request):
        context = {"candle_interval_sec": get_settings().strategies.active.candle_interval_sec}
        return templates.TemplateResponse(request, "index.html", context)

    return fastapi_app


app = create_app()

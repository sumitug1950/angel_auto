"""FastAPI dashboard - the main long-running process. Owns the TradingApp lifecycle
(starts it on startup, stops it cleanly on shutdown); every route reads/writes through
that one shared instance (see dashboard/state.py).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from angel_auto.core.app import TradingApp
from angel_auto.dashboard.state import app_state
from angel_auto.logging_conf import configure_logging, get_logger
from angel_auto.settings import get_settings

log = get_logger(__name__)

DASHBOARD_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(DASHBOARD_DIR / "templates"))


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    configure_logging()
    settings = get_settings()
    trading_app = TradingApp(settings)
    trading_app.start()
    app_state["trading_app"] = trading_app
    log.info("dashboard_startup_complete")
    yield
    trading_app.stop()
    app_state["trading_app"] = None
    log.info("dashboard_shutdown_complete")


def create_app() -> FastAPI:
    fastapi_app = FastAPI(title="angel_auto dashboard", lifespan=lifespan)
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
        return templates.TemplateResponse(request, "index.html", {})

    return fastapi_app


app = create_app()

"""Dashboard route tests - hits the real API routes but with a lightweight stand-in for
TradingApp (no real broker login/WebSocket), so these run fully offline like every other
test. Live end-to-end verification (real login, real WS, real dashboard process) was done
manually via scripts/run_dashboard.py - see the conversation this was built from.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import angel_auto.dashboard.state as dashboard_state
from angel_auto.core.enums import Direction, ExitReason, OrderSide, StructureType
from angel_auto.data.instruments import Instrument, InstrumentMaster
from angel_auto.data.market_data import BarAggregator, OptionChainSnapshot
from angel_auto.persistence import journal
from angel_auto.settings import get_settings
from angel_auto.strategy.macd_itm_otm_spread import MacdItmOtmSpreadStrategy

LOT_SIZE = 65
UNDERLYING = "NIFTY"
STRIKES = [24400.0, 24500.0, 24600.0, 24700.0, 24800.0, 24900.0, 25000.0, 25100.0, 25200.0]
CE_DELTAS = {24600.0: 0.70, 25100.0: 0.10}


def _fake_instruments(expiry: str) -> InstrumentMaster:
    master = InstrumentMaster()
    instruments = []
    token = 1
    for strike in STRIKES:
        for opt_type in ("CE", "PE"):
            instruments.append(
                Instrument(
                    token=str(token), symbol=f"NIFTY{expiry}{int(strike)}{opt_type}", name=UNDERLYING,
                    expiry=expiry, strike=strike, lot_size=LOT_SIZE, instrument_type="OPTIDX", exchange="NFO",
                )
            )
            token += 1
    master._by_name_type_expiry = {(UNDERLYING, "OPTIDX", expiry): instruments}
    master._loaded = True
    return master


class _FakeScheduler:
    tz = timezone.utc


class _FakeTradingApp:
    """Exposes exactly the attribute surface dashboard/api_routes.py and ws_routes.py use."""

    def __init__(self):
        self.settings = get_settings()
        self.instruments = _fake_instruments("29SEP2026")
        self.bars = BarAggregator(interval_sec=15)
        base = datetime(2026, 8, 17, 9, 15, tzinfo=timezone.utc)
        for i in range(30):
            self.bars.add_tick(24700 + i * 3, base + timedelta(seconds=i * 15))  # bullish drift

        self.option_chain = OptionChainSnapshot()
        for strike, delta in CE_DELTAS.items():
            token = str(int(strike))
            self.option_chain.register(token, f"NIFTY29SEP2026{int(strike)}CE", strike, "CE", expiry="29SEP2026")
            self.option_chain.update_ltp(token, 100.0)
            self.option_chain.get(token).delta = delta

        self.strategy = MacdItmOtmSpreadStrategy(
            config=self.settings.strategies.active, underlying=UNDERLYING, lot_size=LOT_SIZE,
            max_trades_per_day=self.settings.app.risk.max_trades_per_day, instruments=self.instruments,
            bar_aggregator=self.bars, option_chain=self.option_chain, get_current_vix=lambda: 15.0,
        )
        self.zero_cross_strategies = {}

        class _Router:
            latest_spot = 24790.0
            latest_vix = 15.0

        self._router = _Router()
        self._scheduler = _FakeScheduler()
        self.oms = None  # not exercised by these tests

    def request_direction(self, direction):
        return self.strategy.on_direction_request(direction)

    def request_structure(self, structure_type):
        self.strategy.on_structure_request(structure_type)

    def cancel_pending(self):
        return self.strategy.cancel_pending_request()

    def manual_exit(self):
        pass

    def kill_switch(self, reason="dashboard kill-switch"):
        from angel_auto.risk import circuit_breaker

        circuit_breaker.trigger_kill_switch(reason)


@pytest.fixture
def client(fresh_db):
    from angel_auto.dashboard.api_routes import router as api_router
    from angel_auto.dashboard.ws_routes import router as ws_router

    test_app = FastAPI()
    test_app.include_router(api_router)
    test_app.include_router(ws_router)

    dashboard_state.app_state["trading_app"] = _FakeTradingApp()
    yield TestClient(test_app)
    dashboard_state.app_state["trading_app"] = None


def test_status_endpoint(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "paper"
    assert data["spot"] == 24790.0
    assert data["vix"] == 15.0
    assert data["open_position"] is None
    assert data["daily_state"]["trades_taken"] == 0
    assert data["structure_preference"] == "DEBIT"  # default before any button is pressed


def test_structure_endpoint_sets_preference(client):
    resp = client.post("/api/structure", json={"structure_type": "CREDIT"})
    assert resp.status_code == 200
    assert resp.json()["structure_preference"] == "CREDIT"

    status = client.get("/api/status").json()
    assert status["structure_preference"] == "CREDIT"


def test_structure_endpoint_rejects_invalid_value(client):
    resp = client.post("/api/structure", json={"structure_type": "SIDEWAYS"})
    assert resp.status_code == 422


def test_direction_request_goes_pending_when_macd_disagrees(client):
    # bars are set up bullish, so SHORT should mismatch and go Pending
    resp = client.post("/api/direction", json={"direction": "SHORT"})
    assert resp.status_code == 200
    assert resp.json()["executed_immediately"] is False

    status = client.get("/api/status").json()
    assert status["pending_request"] is not None
    assert status["pending_request"]["direction"] == "SHORT"


def test_direction_request_executes_immediately_when_macd_matches(client):
    resp = client.post("/api/direction", json={"direction": "LONG"})
    assert resp.status_code == 200
    assert resp.json()["executed_immediately"] is True


def test_cancel_pending_endpoint(client):
    client.post("/api/direction", json={"direction": "SHORT"})
    resp = client.post("/api/cancel-pending")
    assert resp.json()["cancelled"] is True
    assert client.get("/api/status").json()["pending_request"] is None


def test_invalid_direction_rejected(client):
    resp = client.post("/api/direction", json={"direction": "SIDEWAYS"})
    assert resp.status_code == 422  # pydantic validation error, not a 500


def test_kill_switch_endpoint_halts_trading(client):
    resp = client.post("/api/kill-switch")
    assert resp.status_code == 200
    daily_state = journal.get_or_create_daily_state()
    assert daily_state["trading_halted"] is True


def test_trades_endpoint_empty_by_default(client):
    resp = client.get("/api/trades")
    assert resp.status_code == 200
    assert resp.json() == []


def test_equity_curve_endpoint(client):
    journal.append_equity_point(1000.0, -200.0, 20800.0)
    resp = client.get("/api/equity-curve")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["total_equity_rs"] == 20800.0


def test_status_without_trading_app_raises_500():
    from angel_auto.dashboard.api_routes import router as api_router

    test_app = FastAPI()
    test_app.include_router(api_router)
    dashboard_state.app_state["trading_app"] = None
    client = TestClient(test_app, raise_server_exceptions=False)
    resp = client.get("/api/status")
    assert resp.status_code == 500

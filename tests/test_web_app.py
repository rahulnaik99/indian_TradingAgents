"""Smoke tests for the local India dashboard/API."""
from fastapi.testclient import TestClient

from tradingagents.web.app import app

client = TestClient(app)


def test_dashboard_is_served() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "TradingAgents" in response.text
    assert "Historical candles" in response.text


def test_health_is_read_only() -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["live_trading_enabled"] is False


def test_universe_endpoint_returns_configured_snapshot() -> None:
    response = client.get("/api/universe", params={"index": "NIFTY 50"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 50
    symbols = {member["symbol"] for member in payload["members"]}
    assert "RELIANCE" in symbols
    assert "SBIN" in symbols


def test_unknown_universe_is_not_silently_substituted() -> None:
    response = client.get("/api/universe", params={"index": "NIFTY 500"})
    assert response.status_code == 404


def test_fyers_history_requires_credentials(monkeypatch) -> None:
    monkeypatch.delenv("FYERS_APP_ID", raising=False)
    monkeypatch.delenv("FYERS_ACCESS_TOKEN", raising=False)
    response = client.get(
        "/api/fyers/history",
        params={"symbol": "SBIN", "start_date": "2026-09-01", "end_date": "2026-09-10", "timeframe": "1d"},
    )
    assert response.status_code == 503
    assert "credentials" in response.json()["detail"].lower()


def test_fyers_history_rejects_non_universe_symbol() -> None:
    response = client.get(
        "/api/fyers/history",
        params={"symbol": "NOTAREALSTOCK", "start_date": "2026-09-01", "end_date": "2026-09-10", "timeframe": "1d"},
    )
    assert response.status_code == 422


def test_config_reports_paper_only_and_timeframes():
    response = client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert data["live_order_placement"] is False
    assert data["paper_trading"] is True
    assert data["core_timeframes"] == ["1d", "4h", "1h"]


def test_agent_graph_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_LANGGRAPH_AGENT_ANALYSIS", raising=False)
    response = client.post("/api/agent-analysis/SBIN")
    assert response.status_code == 403


def test_moneycontrol_endpoint_is_permission_gated(monkeypatch):
    monkeypatch.delenv("MONEYCONTROL_PERMISSION_CONFIRMED", raising=False)
    response = client.get("/api/fundamentals/moneycontrol/fetch", params={"url": "https://www.moneycontrol.com/financials/test"})
    assert response.status_code == 403


def test_fundamental_source_guidance_mentions_moneycontrol_permission():
    response = client.get("/api/fundamentals/source-guidance")
    assert response.status_code == 200
    data = response.json()
    assert data["moneycontrol_automated_collection_enabled"] is False
    assert "NSE financial results" in data["sources"]


def test_live_order_endpoint_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    monkeypatch.delenv("LIVE_TRADING_CONFIRMATION", raising=False)
    response = client.post("/api/live/orders", json={
        "symbol": "SBIN", "side": "buy", "quantity": 1, "limit_price": 500,
        "confirm_live_order": "PLACE_REAL_ORDER",
    })
    assert response.status_code == 403


def test_integrated_research_requires_fyers_credentials(monkeypatch):
    monkeypatch.delenv("FYERS_APP_ID", raising=False)
    monkeypatch.delenv("FYERS_ACCESS_TOKEN", raising=False)
    response = client.post("/api/research/SBIN", json={"refresh_news": False, "use_llm": False})
    assert response.status_code == 503

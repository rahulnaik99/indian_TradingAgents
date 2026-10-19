from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from tradingagents.india.evidence_package import build_evidence_package, serialize_agent_context
from tradingagents.web import app as app_module


IST = ZoneInfo("Asia/Kolkata")


def candle(day, close, volume, complete=True):
    close_time = datetime.fromisoformat(f"{day}T15:30:00+05:30")
    return SimpleNamespace(
        open_time=close_time - timedelta(hours=6, minutes=15),
        close_time=close_time,
        close=close,
        volume=volume,
        is_complete=complete,
    )


def test_daily_market_snapshot_uses_only_completed_candles():
    as_of = datetime.fromisoformat("2026-10-15T17:00:00+05:30")
    candles = (
        candle("2026-10-13", 100, 1000),
        candle("2026-10-14", 105, 2000),
        candle("2026-10-15", 120, 9000, complete=False),
    )
    result = app_module._daily_market_snapshot("SBIN", candles, as_of)
    assert result["market_snapshot_status"] == "ok"
    assert result["session_date"] == "2026-10-14"
    assert result["close"] == 105
    assert result["change_pct"] == 5.0
    assert result["volume"] == 2000


def test_market_context_uses_only_scan_snapshot_known_by_as_of(monkeypatch):
    monkeypatch.setattr(app_module.STORE, "list_scan_jobs", lambda **kwargs: [
        {"job_id": "future", "updated_at": "2026-10-15T12:00:00+00:00", "result": {"as_of": "2026-10-15T12:00:00+00:00", "results": []}},
        {"job_id": "past", "updated_at": "2026-10-15T08:00:00+00:00", "result": {
            "as_of": "2026-10-15T08:00:00+00:00", "count_requested": 2, "count_errors": 0,
            "results": [
                {"symbol": "SBIN", "market_snapshot_status": "ok", "session_date": "2026-10-14", "close": 105,
                 "change_pct": 5.0, "volume": 2000, "relative_volume_20_sessions": 1.4,
                 "traded_value_estimate": 210000, "multi_timeframe_bias": "bullish_alignment", "aggregate_trend_score": 0.7},
                {"symbol": "INFY", "market_snapshot_status": "ok", "session_date": "2026-10-14", "close": 95,
                 "change_pct": -2.0, "volume": 9000, "relative_volume_20_sessions": 2.0,
                 "traded_value_estimate": 855000, "multi_timeframe_bias": "mixed", "aggregate_trend_score": 0.1},
            ]}}
    ])
    result = app_module._market_context_for_analysis("SBIN", datetime.fromisoformat("2026-10-15T14:00:00+05:30"))
    assert result["status"] == "available"
    assert result["job_id"] == "past"
    assert result["selected_stock"]["symbol"] == "SBIN"
    assert result["market_breadth"] == {"advancers": 1, "decliners": 1, "unchanged": 0, "coverage_count": 2}
    assert result["highest_volume"][0]["symbol"] == "INFY"


def test_market_context_is_carried_into_agent_evidence():
    package = build_evidence_package(
        symbol="SBIN", as_of="2026-10-15T14:00:00+05:30", technical={"timeframes": {}},
        events=[], facts=[], market_context={"status": "available", "market_breadth": {"advancers": 20, "decliners": 10},
                                               "selected_stock": {"symbol": "SBIN", "change_pct": 1.2}},
    )
    serialized = serialize_agent_context(package)
    assert package["market_context"]["selected_stock"]["symbol"] == "SBIN"
    assert '"market_context"' in serialized
    assert '"advancers":20' in serialized


def test_market_movers_endpoint_does_not_trigger_provider_calls(monkeypatch):
    monkeypatch.setattr(app_module.STORE, "list_scan_jobs", lambda **kwargs: [])
    client = TestClient(app_module.app)
    response = client.get("/api/market/movers")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert "no provider request was triggered" in payload["note"].lower()

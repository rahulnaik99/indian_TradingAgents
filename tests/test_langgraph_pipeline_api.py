from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from tradingagents.india.langgraph_pipeline.api import create_app
from tradingagents.india.langgraph_pipeline.persistence import AnalysisStore


class FakeGraph:
    def invoke(self, initial):
        return {
            **initial,
            "run_id": "test-analysis-id",
            "status": "completed",
            "final_verdict": {
                "verdict": "NO_TRADE",
                "rationale": ["Test fixture only"],
                "supporting_evidence": [],
                "contradicting_evidence": [],
                "risk_status": "not_approved",
                "confidence": 0.0,
                "data_quality": "limited",
                "live_order_placed": False,
            },
            "workflow_trace": ["fake_graph"],
            "errors": [],
        }


def fake_graph_factory(**kwargs):
    return FakeGraph()


def request_body():
    return {"symbol": "RELIANCE", "exchange": "NSE", "as_of": "2026-10-16T15:30:00+05:30",
            "timeframes": ["1d", "1h"], "analysis_mode": "historical"}


def test_health_and_order_execution_disabled(tmp_path: Path):
    client = TestClient(create_app(store=AnalysisStore(tmp_path / "api.sqlite"), graph_factory=fake_graph_factory))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["order_execution_enabled"] is False
    assert response.json()["live_gtt_submission_ready"] is False
    assert response.json()["portfolio_access_enabled"] is False
    assert response.json()["execution_provider"] == "disabled"


def test_execution_stays_disabled_even_if_place_order_env_is_true(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PLACE_ORDER", "true")
    client = TestClient(create_app(store=AnalysisStore(tmp_path / "api.sqlite"), graph_factory=fake_graph_factory))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["place_order_flag"] is True
    assert response.json()["order_execution_enabled"] is False
    assert response.json()["live_gtt_submission_ready"] is False


def test_create_and_retrieve_analysis(tmp_path: Path):
    store = AnalysisStore(tmp_path / "api.sqlite")
    client = TestClient(create_app(store=store, graph_factory=fake_graph_factory))
    created = client.post("/analyses", json=request_body())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["analysis_id"] == "test-analysis-id"
    assert body["final_verdict"]["verdict"] == "NO_TRADE"
    assert body["live_order_placed"] is False
    assert body["execution_result"]["status"] == "EXECUTION_DISABLED"
    assert body["execution_result"]["submitted"] is False
    assert client.get(f"/analyses/{body['analysis_id']}").status_code == 200
    assert client.get("/analyses?symbol=RELIANCE").json()["items"][0]["symbol"] == "RELIANCE"


def test_invalid_timestamp_is_rejected(tmp_path: Path):
    client = TestClient(create_app(store=AnalysisStore(tmp_path / "api.sqlite"), graph_factory=fake_graph_factory))
    body = request_body()
    body["as_of"] = "2026-10-16T15:30:00"
    response = client.post("/analyses", json=body)
    assert response.status_code == 422


def test_analysis_qa_is_persisted(tmp_path: Path):
    store = AnalysisStore(tmp_path / "api.sqlite")
    client = TestClient(create_app(store=store, graph_factory=fake_graph_factory))
    created = client.post("/analyses", json=request_body()).json()
    analysis_id = created["analysis_id"]
    response = client.post(f"/analyses/{analysis_id}/questions", json={"question": "Why no trade?"})
    assert response.status_code == 200
    assert response.json()["analysis_id"] == analysis_id
    history = client.get(f"/analyses/{analysis_id}/questions").json()["items"]
    assert len(history) == 1


def test_portfolio_endpoint_is_disabled_without_calling_kite(tmp_path: Path):
    client = TestClient(create_app(store=AnalysisStore(tmp_path / "api.sqlite"), graph_factory=fake_graph_factory))
    response = client.get("/portfolio")
    assert response.status_code == 503
    assert "temporarily disabled" in response.json()["detail"]

import threading
import time
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import tradingagents.web.app as app_module
from tradingagents.india.agent_tool_adapter import india_tool_result, use_india_evidence
from tradingagents.india.evidence_package import build_evidence_package, serialize_agent_context
from tradingagents.india.fundamentals_view import (
    build_statement_view, last_refreshed, statement_text, statement_view, statements_from_facts,
)
from tradingagents.india.statements import refresh_yahoo_statements
from tradingagents.india.storage import IndiaStore

TODAY = date(2026, 10, 10)
A = ["2026-03-31", "2025-03-31", "2024-03-31"]
Q = ["2026-06-30", "2026-03-31"]
INC = [("Total Revenue", 2.67e12), ("Net Income", 4.92e11), ("Diluted EPS", 136.01), ("EBITDA", 7.1e11)]
BAL = [("Total Assets", 1.82e12), ("Total Debt", 1.1e11)]
CF = [("Operating Cash Flow", 5.2e11)]


def _fr(rows, ends):
    return pd.DataFrame({pd.Timestamp(e): [r[1] for r in rows] for e in ends}, index=[r[0] for r in rows])


class FakeTicker:
    def __init__(self, symbol): pass
    income_stmt, quarterly_income_stmt = _fr(INC, A), _fr(INC, Q)
    balance_sheet, quarterly_balance_sheet = _fr(BAL, A), _fr(BAL, Q)
    cashflow, quarterly_cashflow = _fr(CF, A), _fr(CF, Q)


@pytest.fixture()
def store(tmp_path):
    st = IndiaStore(tmp_path / "v.sqlite3")
    refresh_yahoo_statements("TCS", st, ticker_factory=FakeTicker, today=TODAY)
    return st


def _statement_facts(store):
    return store.list_facts("TCS", 2000)


# ------------------------------------------------------------------ view helpers
def test_statement_view_converts_to_crore_and_orders_periods(store):
    view = statement_view(store, "TCS", "annual")
    assert view["periods"] == A  # newest first
    income = {r["metric"]: r for r in view["sections"]["income"]}
    assert income["REVENUE"]["values"]["2026-03-31"] == 267000.0  # 2.67e12 INR = 267,000 crore
    assert income["EPS_DILUTED"]["unit"] == "INR per share" and income["EPS_DILUTED"]["values"]["2026-03-31"] == 136.01
    assert [r["metric"] for r in view["sections"]["balance"]] == ["TOTAL_ASSETS", "TOTAL_DEBT"]
    assert statement_view(store, "TCS", "quarterly")["periods"] == Q


def test_last_refreshed_per_source(store):
    lr = last_refreshed(store, "TCS")
    assert lr["statements"] and lr["snapshot"] is None
    assert last_refreshed(store, "NOPE") == {"statements": None, "snapshot": None}


def test_statement_text_respects_frequency_and_section(store):
    statements = statements_from_facts(_statement_facts(store))
    income = statement_text(statements, "income", "annual", symbol="TCS", as_of="2026-10-10T10:00:00+05:30")
    assert "EPS (diluted) [INR per share]: 136.01 | 136.01 | 136.01" in income
    assert "Total assets" not in income and "Operating cash flow" not in income
    assert "2026-06-30" in statement_text(statements, "balance", "quarterly", symbol="TCS", as_of="x")
    assert "not official filings" in income
    assert statement_text({"annual": statements["annual"]}, "income", "quarterly", symbol="TCS", as_of="x") is None


# -------------------------------------------------------------- evidence package
def _package(store, as_of):
    return build_evidence_package(symbol="TCS", as_of=as_of, technical={"timeframes": {"1d": {"status": "ok", "count": 1}}},
                                  events=[], facts=_statement_facts(store) + [
        {"metric_code": "PE_RATIO", "value": 20.0, "unit": "ratio", "period_end": "2026-10-10", "scope": "unspecified",
         "published_at": "2026-10-10T20:00:00+05:30"}])


def test_package_moves_statements_out_of_flat_facts(store):
    package = _package(store, "2026-10-11T10:00:00+05:30")
    assert [f["metric_code"] for f in package["financial_facts"]] == ["PE_RATIO"]
    assert package["financial_statements"]["annual"]["periods"] == A
    assert len(serialize_agent_context(package)) < 24000


def test_package_hides_statements_before_retrieval_time(store):
    package = _package(store, "2020-01-01T10:00:00+05:30")
    assert package["financial_statements"] is None
    assert package["financial_facts"] == []


# ---------------------------------------------------------------- agent adapter
def test_adapter_returns_frequency_specific_statement_tables(store):
    package = _package(store, "2026-10-11T10:00:00+05:30")
    with use_india_evidence(package):
        annual = india_tool_result("get_income_statement", "TCS", "annual", "2026-10-10")
        quarterly = india_tool_result("get_balance_sheet", "TCS", "quarterly", "2026-10-10")
        cash = india_tool_result("get_cashflow", "TCS", "annual", "2026-10-10")
    assert "EBITDA" in annual and "EPS (diluted)" in annual and "2025-03-31" in annual
    assert "Total assets" in quarterly and "2026-06-30" in quarterly and "Operating cash flow" not in quarterly
    assert "Operating cash flow" in cash and "Total assets" not in cash


def test_adapter_fails_closed_when_statements_hidden_by_cutoff(store):
    package = _package(store, "2020-01-01T10:00:00+05:30")
    with use_india_evidence(package):
        # Falls back to the flat-fact path, which also has no eligible statement facts.
        assert india_tool_result("get_income_statement", "TCS", "annual", "2020-01-01").startswith("INDIA_DATA_UNAVAILABLE")


# ------------------------------------------------------------------- endpoints
@pytest.fixture()
def client(store, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "STORE", store)
    monkeypatch.setattr(app_module, "_FUND_PROGRESS_PATH", tmp_path / "progress.jsonl")
    monkeypatch.setattr(app_module, "_FUND_COVERAGE_PATH", tmp_path / "coverage.csv")
    monkeypatch.setattr(app_module, "_nse_equity_catalog", lambda *a, **k: [{"symbol": "TCS"}])
    app_module._FUND_JOB.clear()
    app_module._FUND_JOB["status"] = "idle"
    return TestClient(app_module.app)


def test_statements_and_freshness_endpoints(client):
    body = client.get("/api/fundamentals/TCS/statements", params={"period_type": "annual"}).json()
    assert body["periods"] == A and body["last_refreshed"]["statements"]
    assert client.get("/api/fundamentals/TCS/statements", params={"period_type": "weekly"}).status_code == 422
    assert client.get("/api/fundamentals/TCS").json()["last_refreshed"]["statements"]


def test_refresh_endpoint_runs_statements_then_snapshot(client, monkeypatch):
    import tradingagents.india.statements as statements_module
    from tradingagents.india.fundamentals import FundamentalDataError

    monkeypatch.setattr(statements_module, "refresh_yahoo_statements",
                        lambda sym, st: refresh_yahoo_statements(sym, st, ticker_factory=FakeTicker, today=TODAY))
    monkeypatch.setattr(app_module, "refresh_yahoo_snapshot", lambda sym, st: {"inserted": 7})
    ok = client.post("/api/fundamentals/TCS/refresh").json()
    assert ok["facts_stored"] > 0 and ok["snapshot_metrics"] == 7 and ok["snapshot_error"] is None
    assert ok["last_refreshed"]["statements"] == ok["refreshed_at"]

    def boom(sym, st): raise FundamentalDataError("Yahoo Finance request failed (X)")
    monkeypatch.setattr(app_module, "refresh_yahoo_snapshot", boom)
    partial = client.post("/api/fundamentals/TCS/refresh").json()  # snapshot failure is non-fatal
    assert partial["snapshot_metrics"] == 0 and "request failed" in partial["snapshot_error"]


def test_refresh_endpoint_reports_provider_failure(client, monkeypatch):
    import tradingagents.india.statements as statements_module

    def fail(sym, st): raise statements_module.StatementDataError("Yahoo returned no statement data for X.NS; no facts were changed.")
    monkeypatch.setattr(statements_module, "refresh_yahoo_statements", fail)
    assert client.post("/api/fundamentals/TCS/refresh").status_code == 502


def test_refresh_all_job_progress_and_single_flight(client, monkeypatch):
    gate = threading.Event()

    def fake_run_bulk(symbols, store, *, on_progress=None, **kw):
        for i, sym in enumerate(symbols, 1):
            gate.wait(5)
            on_progress(i, len(symbols), sym, "ok" if sym != "BAD" else "failed")
        return {"halted": False}

    monkeypatch.setattr(app_module, "run_bulk", fake_run_bulk)
    monkeypatch.setattr(app_module, "_fundamentals_universe", lambda: ("NIFTY 500", ["AAA", "BAD", "CCC"]))
    started = client.post("/api/fundamentals/refresh-all").json()
    assert started["status"] == "running" and started["total"] == 3
    assert client.post("/api/fundamentals/refresh-all").status_code == 409  # one at a time
    assert client.get("/api/fundamentals/refresh-all/status").json()["job"]["status"] == "running"
    gate.set()
    for _ in range(100):
        job = client.get("/api/fundamentals/refresh-all/status").json()["job"]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "finished" and (job["done"], job["failed"], job["processed"]) == (2, 1, 3)
    assert client.post("/api/fundamentals/refresh-all").status_code == 200  # can start again after finishing
    gate.set()


def test_dashboard_has_refresh_controls_and_freshness_line(client):
    html = client.get("/").text
    for element in ("refreshFundamentals", "refreshAllFundamentals", "factsFresh", "statementsTable", "stmtQuarterly"):
        assert f'id="{element}"' in html

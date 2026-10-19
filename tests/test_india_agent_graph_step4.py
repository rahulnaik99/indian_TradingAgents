"""Step 4 integration-contract tests around the original graph runner.

These tests mock TradingAgentsGraph at its boundary because the bare validation
container does not install LangGraph/LangChain provider dependencies. They still
exercise the real run_existing_graph scope, India tool adapter and SQLite audit.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from tradingagents.india import agent_runner, execution_audit as audit
from tradingagents.india.agent_tool_adapter import active_package, india_tool_result


def sample_package():
    return {
        "schema_version": "1.0", "symbol": "SBIN", "exchange": "NSE",
        "as_of": "2026-10-09T15:30:00+05:30",
        "market_data": {"provider": "fyers", "technical_snapshot": {
            "timeframes": {"1d": {"status": "ok", "count": 250, "ema20": 800.0, "rsi14": 62.0}}
        }},
        "news_and_events": [
            {"published_at_utc": "2026-10-09T09:59:00+00:00", "title": "Eligible before cutoff",
             "summary": "Published before cutoff", "event_type": "company_news", "source_url": "https://nse.example/old"},
            {"published_at_utc": "2026-10-09T10:01:00+00:00", "title": "Future after cutoff",
             "summary": "Published after cutoff", "event_type": "company_news", "source_url": "https://nse.example/future"},
            {"published_at": "2026-10-09", "title": "Date only is unknown", "source_url": "https://nse.example/undated"},
        ],
        "financial_facts": [
            {"metric_code": "net_profit", "value": 123, "unit": "INR_crore", "currency": "INR",
             "period_start": "2026-04-01", "period_end": "2026-06-30", "period_type": "quarter",
             "scope": "consolidated", "published_at_utc": "2026-08-01T04:30:00+00:00", "source_url": "https://nse.example/fact"},
            {"metric_code": "net_profit", "value": 999, "unit": "INR_crore", "currency": "INR",
             "period_start": "2026-04-01", "period_end": "2026-06-30", "period_type": "quarter",
             "scope": "consolidated", "published_at_utc": "2026-10-09T10:01:00+00:00", "source_url": "https://nse.example/future-fact"},
        ],
    }


def _install_fake_graph(monkeypatch, fake_graph):
    package = types.ModuleType("tradingagents.graph")
    package.__path__ = []
    module = types.ModuleType("tradingagents.graph.trading_graph")
    module.TradingAgentsGraph = fake_graph
    monkeypatch.setitem(sys.modules, "tradingagents.graph", package)
    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", module)


def _install_fake_callbacks(monkeypatch):
    root = types.ModuleType("langchain_core")
    root.__path__ = []
    callbacks = types.ModuleType("langchain_core.callbacks")
    class BaseCallbackHandler:
        def __init__(self):
            pass
    callbacks.BaseCallbackHandler = BaseCallbackHandler
    monkeypatch.setitem(sys.modules, "langchain_core", root)
    monkeypatch.setitem(sys.modules, "langchain_core.callbacks", callbacks)


def _valid_state():
    return ({
        "market_report": "market report", "sentiment_report": "sentiment report",
        "news_report": "news report", "fundamentals_report": "fundamentals report",
        "investment_plan": "plan", "trader_investment_plan": "proposal",
        "final_trade_decision": "HOLD", "investment_debate_state": {"bull_history": "bull", "bear_history": "bear"},
        "risk_debate_state": {"aggressive_history": "a", "conservative_history": "c", "neutral_history": "n"},
    }, "HOLD")


def test_graph_runner_keeps_india_package_active_during_propagate_and_resets_after(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "DB_PATH", tmp_path / "audit.sqlite3")
    _install_fake_callbacks(monkeypatch)
    observed = {}

    class FakeGraph:
        def __init__(self, selected_analysts, debug, config):
            pass
        def propagate(self, ticker, trade_date, **kwargs):
            package = active_package()
            assert package and package["symbol"] == "SBIN"
            observed["ticker"] = ticker
            observed["news"] = india_tool_result("get_news", ticker, "2026-10-01", "2026-10-09")
            observed["indicators"] = india_tool_result("get_indicators", ticker, "rsi", "2026-10-09", 30)
            observed["context"] = kwargs["initial_context"]
            callback = kwargs["callbacks"][0]
            callback.on_chain_start({}, {}, run_id="agent-1", metadata={"langgraph_node": "market_analyst"})
            callback.on_chain_end({"report": "market"}, run_id="agent-1")
            callback.on_tool_start({"name": "get_news"}, "SBIN", run_id="tool-1")
            callback.on_tool_end(observed["news"], run_id="tool-1")
            return _valid_state()
        def save_reports(self, state, ticker):
            return "/tmp/mock-report.html"
        def close(self):
            observed["closed"] = True

    _install_fake_graph(monkeypatch, FakeGraph)
    run_id = audit.create_run("upstream_agent_graph", "SBIN")
    result = agent_runner.run_existing_graph(
        "SBIN", "2026-10-09", analysts=("market", "social", "news", "fundamentals"),
        run_id=run_id, explicit_user_request=True, external_context=json.dumps(sample_package()),
    )

    assert observed["ticker"] == "SBIN.NS"
    assert "Eligible before cutoff" in observed["news"]
    assert "Future after cutoff" not in observed["news"]
    assert "Date only is unknown" not in observed["news"]
    assert "rsi14=62.0" in observed["indicators"]
    assert "financial_facts" in observed["context"]
    assert result["decision"] == "HOLD"
    assert observed["closed"] is True
    assert active_package() is None, "India evidence scope must be reset after graph execution"

    run = audit.get_run(run_id)
    by_id = {row["component_id"]: row for row in run["components"]}
    assert by_id["upstream_market_analyst"]["status"] == "completed"
    assert by_id["upstream_tool_get_news"]["status"] == "completed"
    assert by_id["upstream_tool_get_news"]["details"]["data_source_mode"] == "india_shared_evidence_adapter"



def test_financial_adapter_filters_future_and_undated_facts_at_exact_cutoff():
    package = sample_package()
    package["financial_facts"].append({
        "metric_code": "net_profit", "value": 777, "unit": "INR_crore",
        "published_at": "2026-10-09", "source_url": "https://nse.example/undated-fact",
    })
    from tradingagents.india.agent_tool_adapter import use_india_evidence
    with use_india_evidence(package):
        result = india_tool_result("get_fundamentals", "SBIN", "2026-10-09", trade_date="2026-10-09")
    assert "value=123" in result
    assert "value=999" not in result
    assert "value=777" not in result


def test_callback_marks_unavailable_tool_and_failed_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "DB_PATH", tmp_path / "audit.sqlite3")
    _install_fake_callbacks(monkeypatch)
    run_id = audit.create_run("upstream_agent_graph", "SBIN")
    callback = agent_runner._make_tool_audit_callback(run_id, india_adapter_active=True)

    callback.on_tool_start({"name": "get_stock_data"}, "SBIN", run_id="tool-2")
    callback.on_tool_end("INDIA_DATA_UNAVAILABLE [get_stock_data]: FYERS unavailable", run_id="tool-2")
    callback.on_chain_start({}, {}, run_id="agent-2", metadata={"langgraph_node": "news_analyst"})
    callback.on_chain_error(RuntimeError("mock provider failure"), run_id="agent-2")

    run = audit.get_run(run_id)
    by_id = {row["component_id"]: row for row in run["components"]}
    assert by_id["upstream_tool_get_stock_data"]["status"] == "unavailable"
    assert by_id["upstream_tool_get_stock_data"]["details"]["data_source_mode"] == "india_shared_evidence_adapter"
    assert by_id["upstream_news_analyst"]["status"] == "failed"
    assert any(event["event_type"] == "tool_unavailable" for event in run["events"])
    assert any(event["event_type"] == "agent_failed" for event in run["events"])


def test_invalid_context_does_not_claim_india_adapter_in_audit(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "DB_PATH", tmp_path / "audit.sqlite3")
    _install_fake_callbacks(monkeypatch)

    class FakeGraph:
        def __init__(self, selected_analysts, debug, config):
            pass
        def propagate(self, ticker, trade_date, **kwargs):
            callback = kwargs["callbacks"][0]
            callback.on_tool_start({"name": "get_news"}, "SBIN", run_id="tool-3")
            callback.on_tool_end("upstream news output", run_id="tool-3")
            assert active_package() is None
            return _valid_state()
        def save_reports(self, state, ticker):
            return None
        def close(self):
            pass

    _install_fake_graph(monkeypatch, FakeGraph)
    run_id = audit.create_run("upstream_agent_graph", "SBIN")
    agent_runner.run_existing_graph(
        "SBIN", "2026-10-09", run_id=run_id, explicit_user_request=True,
        external_context="not-json",
    )
    run = audit.get_run(run_id)
    tool = next(row for row in run["components"] if row["component_id"] == "upstream_tool_get_news")
    assert tool["status"] == "completed"
    assert tool["details"]["data_source_mode"] == "upstream_provider"


def test_context_scope_resets_even_when_graph_raises(monkeypatch):
    class FakeGraph:
        def __init__(self, selected_analysts, debug, config):
            self.closed = False
        def propagate(self, ticker, trade_date, **kwargs):
            assert active_package() is not None
            raise RuntimeError("mock graph failure")
        def close(self):
            self.closed = True

    _install_fake_graph(monkeypatch, FakeGraph)
    with pytest.raises(RuntimeError, match="mock graph failure"):
        agent_runner.run_existing_graph(
            "SBIN", "2026-10-09", explicit_user_request=True,
            external_context=json.dumps(sample_package()),
        )
    assert active_package() is None

"""Regression tests for restoring the original analyst team to Full Analysis."""
from __future__ import annotations

import sys
import types

from tradingagents.india import agent_runner


def _install_fake_graph_module(monkeypatch, fake_graph):
    package = types.ModuleType("tradingagents.graph")
    package.__path__ = []
    module = types.ModuleType("tradingagents.graph.trading_graph")
    module.TradingAgentsGraph = fake_graph
    monkeypatch.setitem(sys.modules, "tradingagents.graph", package)
    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", module)


def test_full_agent_runner_includes_sentiment_report_and_stage(monkeypatch):
    captured = {}

    class FakeGraph:
        def __init__(self, selected_analysts, debug, config):
            captured["selected_analysts"] = tuple(selected_analysts)

        def propagate(self, ticker, trade_date, **kwargs):
            captured["initial_context"] = kwargs.get("initial_context")
            return ({
                "market_report": "market ok",
                "sentiment_report": "sentiment ok",
                "news_report": "news ok",
                "fundamentals_report": "fundamentals ok",
                "investment_plan": "plan ok",
                "trader_investment_plan": "proposal ok",
                "final_trade_decision": "HOLD",
                "investment_debate_state": {"bull_history": "bull ok", "bear_history": "bear ok"},
                "risk_debate_state": {"aggressive_history": "aggressive ok", "conservative_history": "conservative ok", "neutral_history": "neutral ok"},
            }, "HOLD")

        def save_reports(self, state, ticker):
            return "/tmp/report"

        def close(self):
            pass

    _install_fake_graph_module(monkeypatch, FakeGraph)
    result = agent_runner.run_existing_graph(
        "RELIANCE", "2026-10-09",
        analysts=("market", "social", "news", "fundamentals"),
        explicit_user_request=True,
        external_context='{"technical": {"daily": "bullish"}}',
    )

    assert captured["selected_analysts"] == ("market", "social", "news", "fundamentals")
    assert "technical" in captured["initial_context"]
    assert result["summaries"]["sentiment_report"] == "sentiment ok"
    social = next(s for s in result["stages"] if s["component_id"] == "upstream_social_analyst")
    assert social["status"] == "completed"


def test_social_stage_is_skipped_only_when_not_selected(monkeypatch):
    class FakeGraph:
        def __init__(self, selected_analysts, debug, config):
            pass

        def propagate(self, ticker, trade_date, **kwargs):
            return ({
                "market_report": "market ok", "news_report": "news ok",
                "fundamentals_report": "fundamentals ok", "investment_plan": "plan ok",
                "trader_investment_plan": "proposal ok", "final_trade_decision": "HOLD",
                "investment_debate_state": {}, "risk_debate_state": {},
            }, "HOLD")

        def save_reports(self, state, ticker):
            return "/tmp/report"

        def close(self):
            pass

    _install_fake_graph_module(monkeypatch, FakeGraph)
    result = agent_runner.run_existing_graph(
        "RELIANCE", "2026-10-09", analysts=("market", "news", "fundamentals"),
        explicit_user_request=True,
    )
    social = next(s for s in result["stages"] if s["component_id"] == "upstream_social_analyst")
    assert social["status"] == "skipped"

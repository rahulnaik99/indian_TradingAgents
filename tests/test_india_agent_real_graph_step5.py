"""Real LangGraph integration test for the India evidence adapters.

This test runs the repository's actual TradingAgentsGraph and compiled
LangGraph StateGraph. The LLM and non-essential data providers are deterministic
mocks; the real market/news/fundamentals tools and their India evidence adapter
remain in the execution path. It is safe for CI and does not make network calls.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tradingagents.india import agent_runner, execution_audit as audit


class DeterministicToolCallingChatModel(BaseChatModel):
    """One-tool-per-analyst fake model; never contacts an external LLM provider."""

    bound_tools: tuple[Any, ...] = ()

    @property
    def _llm_type(self) -> str:
        return "india-graph-integration-test"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return DeterministicToolCallingChatModel(bound_tools=tuple(tools))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        tool_names = [getattr(tool, "name", "") for tool in self.bound_tools]
        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        if tool_names and not tool_messages:
            selected = next((name for name in ("get_indicators", "get_news", "get_fundamentals") if name in tool_names), None)
            if selected:
                args = {
                    "get_indicators": {"indicator": "rsi", "curr_date": "2026-10-09", "look_back_days": 30},
                    "get_news": {"start_date": "2026-10-01", "end_date": "2026-10-09"},
                    "get_fundamentals": {"curr_date": "2026-10-09"},
                }[selected]
                message = AIMessage(
                    content="",
                    tool_calls=[{"name": selected, "args": args, "id": f"call-{selected}", "type": "tool_call"}],
                )
                return ChatResult(generations=[ChatGeneration(message=message)])
        if tool_messages:
            last_result = str(tool_messages[-1].content)
            content = f"Deterministic analyst report based on tool output:\n{last_result}"
        else:
            content = "Deterministic mock reasoning: weigh available evidence and disclose uncertainty."
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def _package() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "symbol": "SBIN",
        "exchange": "NSE",
        "as_of": "2026-10-09T15:30:00+05:30",
        "market_data": {
            "provider": "fyers",
            "technical_snapshot": {
                "timeframes": {
                    "1d": {"status": "ok", "count": 250, "latest_close": 812.5, "rsi14": 62.0}
                }
            },
        },
        "news_and_events": [
            {"published_at_utc": "2026-10-09T09:59:00+00:00", "title": "Eligible NSE event",
             "summary": "Published before cutoff", "event_type": "company_news", "source_url": "https://nse.example/eligible"},
            {"published_at_utc": "2026-10-09T10:01:00+00:00", "title": "Future NSE event",
             "summary": "Published after cutoff", "event_type": "company_news", "source_url": "https://nse.example/future"},
            {"published_at": "2026-10-09", "title": "Undated NSE event", "source_url": "https://nse.example/undated"},
        ],
        "financial_facts": [
            {"metric_code": "net_profit", "value": 123, "unit": "INR_crore", "currency": "INR",
             "period_start": "2026-04-01", "period_end": "2026-06-30", "period_type": "quarter",
             "scope": "consolidated", "published_at_utc": "2026-08-01T04:30:00+00:00",
             "source_url": "https://nse.example/fact"},
            {"metric_code": "net_profit", "value": 999, "unit": "INR_crore", "currency": "INR",
             "period_start": "2026-04-01", "period_end": "2026-06-30", "period_type": "quarter",
             "scope": "consolidated", "published_at_utc": "2026-10-09T10:01:00+00:00",
             "source_url": "https://nse.example/future-fact"},
        ],
    }


def _mock_node_factory(key: str):
    """Patch only selected non-data decision nodes; graph topology stays real."""
    def factory(_llm):
        def node(state):
            if key == "sentiment":
                return {"sentiment_report": "Mock sentiment report: sources unavailable in this deterministic test."}
            if key == "research_manager":
                return {"investment_plan": "Recommendation: Hold\nRationale: deterministic test plan."}
            if key == "trader":
                return {"trader_investment_plan": "Action: Hold\nReasoning: deterministic test proposal."}
            if key == "portfolio_manager":
                return {"final_trade_decision": "**Rating**: Hold\nMock final decision.", "final_rating": "Hold"}
            raise AssertionError(f"unexpected mocked node factory: {key}")
        return node
    return factory


def test_actual_langgraph_routes_shared_india_evidence_through_real_tools(monkeypatch, tmp_path):
    """Exercise the real graph executor and ToolNode with deterministic providers."""
    from tradingagents import default_config
    from tradingagents.graph import setup as graph_setup
    from tradingagents.graph import trading_graph
    from tradingagents.agents import tools as agent_tools

    # Isolate all outputs and avoid memory settlement, identity lookups, and any network IO.
    config = default_config.DEFAULT_CONFIG.copy()
    config.update({
        "results_dir": str(tmp_path / "results"),
        "data_cache_dir": str(tmp_path / "cache"),
        "memory_log_path": str(tmp_path / "memory.md"),
        "checkpoint_enabled": False,
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "max_tool_rounds": 3,
        "max_recur_limit": 100,
    })
    monkeypatch.setattr(default_config, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda *a, **k: type("Client", (), {"get_llm": lambda self: DeterministicToolCallingChatModel()})())
    monkeypatch.setattr(trading_graph.TradingAgentsGraph, "resolve_instrument_context", lambda *a, **k: "NSE instrument SBIN; identity fixed for integration test")
    monkeypatch.setattr(trading_graph.TradingAgentsGraph, "_memory_step", lambda self, state: {"past_context": "", "memory_note": ""})
    monkeypatch.setattr(trading_graph.TradingAgentsGraph, "record_decision", lambda *a, **k: None)

    # Actual market/news/fundamentals analyst factories remain active. The real
    # analyst ToolNodes and tools are therefore exercised within the compiled graph.
    monkeypatch.setattr(graph_setup, "create_sentiment_analyst", _mock_node_factory("sentiment"))
    monkeypatch.setattr(graph_setup, "create_research_manager", _mock_node_factory("research_manager"))
    monkeypatch.setattr(graph_setup, "create_trader", _mock_node_factory("trader"))
    monkeypatch.setattr(graph_setup, "create_portfolio_manager", _mock_node_factory("portfolio_manager"))

    # If context propagation fails, prevent the tools from falling through to any live provider.
    monkeypatch.setattr(agent_tools, "route_to_vendor", lambda *a, **k: "UPSTREAM_FALLBACK_USED")
    monkeypatch.setattr(agent_tools, "build_verified_market_snapshot", lambda *a, **k: "UPSTREAM_SNAPSHOT_USED")

    monkeypatch.setattr(audit, "DB_PATH", tmp_path / "audit.sqlite3")
    package = _package()
    run_id = audit.create_run("upstream_agent_graph", "SBIN")

    result = agent_runner.run_existing_graph(
        "SBIN", "2026-10-09", analysts=("market", "social", "news", "fundamentals"),
        run_id=run_id, explicit_user_request=True, external_context=json.dumps(package),
    )

    assert result["decision"] == "Hold"
    assert "rsi14=62.0" in result["summaries"]["market_report"]
    assert "Eligible NSE event" in result["summaries"]["news_report"]
    assert "Future NSE event" not in result["summaries"]["news_report"]
    assert "Undated NSE event" not in result["summaries"]["news_report"]
    assert "value=123" in result["summaries"]["fundamentals_report"]
    assert "value=999" not in result["summaries"]["fundamentals_report"]
    assert "UPSTREAM_FALLBACK_USED" not in json.dumps(result["summaries"])

    run = audit.get_run(run_id)
    components = {row["component_id"]: row for row in run["components"]}
    for component_id in (
        "upstream_market_analyst", "upstream_social_analyst", "upstream_news_analyst",
        "upstream_fundamentals_analyst", "bull_researcher", "bear_researcher",
        "research_manager", "trader_agent", "aggressive_analyst", "conservative_analyst",
        "neutral_analyst", "portfolio_manager",
    ):
        assert components[component_id]["status"] == "completed", component_id
    for tool_id in ("upstream_tool_get_indicators", "upstream_tool_get_news", "upstream_tool_get_fundamentals"):
        assert components[tool_id]["status"] == "completed", tool_id
        assert components[tool_id]["details"]["data_source_mode"] == "india_shared_evidence_adapter"

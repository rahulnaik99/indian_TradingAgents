"""Adapter for the repository's original LangGraph TradingAgents graph.

Interactive Full Analysis explicitly invokes the graph and may incur provider
costs. Non-interactive/background callers must separately opt in via environment.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from tradingagents.india.tracing import traced
from tradingagents.india.execution_audit import mark_component
from tradingagents.india.agent_tool_adapter import is_india_evidence_package, use_india_evidence

logger = logging.getLogger(__name__)


class AgentRunDisabled(RuntimeError):
    pass


def _make_tool_audit_callback(run_id: str, *, india_adapter_active: bool = False):
    """Record live upstream graph-node and tool lifecycle events when callbacks expose them."""
    from langchain_core.callbacks import BaseCallbackHandler

    class ToolAuditCallback(BaseCallbackHandler):
        def __init__(self):
            super().__init__()
            self._tool_runs: dict[str, tuple[str, str]] = {}
            self._agent_runs: dict[str, tuple[str, str]] = {}

        @staticmethod
        def _agent_component(serialized, metadata):
            metadata = metadata or {}
            raw_name = metadata.get("langgraph_node") or (serialized or {}).get("name") or ""
            normalized = " ".join(str(raw_name).replace("_", " ").replace("-", " ").split()).casefold()
            mapping = {
                "market analyst": "upstream_market_analyst",
                "sentiment analyst": "upstream_social_analyst",
                "news analyst": "upstream_news_analyst",
                "fundamentals analyst": "upstream_fundamentals_analyst",
                "bull researcher": "bull_researcher",
                "bear researcher": "bear_researcher",
                "research manager": "research_manager",
                "trader": "trader_agent",
                "aggressive analyst": "aggressive_analyst",
                "conservative analyst": "conservative_analyst",
                "neutral analyst": "neutral_analyst",
                "portfolio manager": "portfolio_manager",
            }
            return mapping.get(normalized), str(raw_name)

        def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
            component_id, node_name = self._agent_component(serialized, metadata)
            if not component_id:
                return
            self._agent_runs[str(run_id)] = (component_id, node_name)
            mark_component(run_id_value, component_id, "running", event_type="agent_started",
                           message=f"Original TradingAgents node {node_name} started",
                           details={"node_name": node_name, "status_basis": "live_langchain_callback"})

        def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
            found = self._agent_runs.pop(str(run_id), None)
            if not found:
                return
            component_id, node_name = found
            mark_component(run_id_value, component_id, "completed", event_type="agent_completed",
                           message=f"Original TradingAgents node {node_name} completed",
                           details={"node_name": node_name, "status_basis": "live_langchain_callback",
                                    "output_preview": str(outputs)[:1200]})

        def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            found = self._agent_runs.pop(str(run_id), None)
            if not found:
                return
            component_id, node_name = found
            mark_component(run_id_value, component_id, "failed", event_type="agent_failed",
                           message=f"Original TradingAgents node {node_name} failed",
                           reason=f"Node raised {type(error).__name__}.",
                           details={"node_name": node_name, "error_type": type(error).__name__,
                                    "status_basis": "live_langchain_callback"})

        def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, metadata=None, inputs=None, **kwargs):
            serialized = serialized or {}
            identifier = serialized.get("id")
            if isinstance(identifier, (list, tuple)):
                identifier = identifier[-1] if identifier else "unknown"
            name = str(serialized.get("name") or identifier or "unknown")
            candidate = "upstream_tool_" + name
            component_id = candidate if candidate in _UPSTREAM_TOOL_IDS else "upstream_other_tool"
            self._tool_runs[str(run_id)] = (component_id, name)
            mark_component(run_id_value, component_id, "running", event_type="tool_started",
                           message=f"Tool {name} started", details={"tool_name": name, "input_preview": str(input_str)[:400]})

        def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
            found = self._tool_runs.pop(str(run_id), None)
            if not found:
                return
            component_id, name = found
            output_text = str(output)
            source_mode = "india_shared_evidence_adapter" if india_adapter_active else "upstream_provider"
            if output_text.startswith("INDIA_DATA_UNAVAILABLE"):
                mark_component(run_id_value, component_id, "unavailable", event_type="tool_unavailable",
                               message=f"Tool {name} had no eligible India-specific data; no silent provider fallback occurred.",
                               reason=output_text.splitlines()[0][:500],
                               details={"tool_name": name, "data_source_mode": source_mode,
                                        "output_preview": output_text[:1200]})
                return
            mark_component(run_id_value, component_id, "completed", event_type="tool_completed",
                           message=f"Tool {name} completed", details={"tool_name": name, "data_source_mode": source_mode,
                                                                       "output_preview": output_text[:1200]})

        def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            found = self._tool_runs.pop(str(run_id), None)
            if not found:
                return
            component_id, name = found
            mark_component(run_id_value, component_id, "failed", event_type="tool_failed",
                           message=f"Tool {name} failed", reason=f"Tool raised {type(error).__name__}.",
                           details={"tool_name": name, "error_type": type(error).__name__})

    run_id_value = run_id
    return ToolAuditCallback()

_UPSTREAM_TOOL_IDS = {
    "upstream_tool_get_stock_data", "upstream_tool_get_indicators", "upstream_tool_get_verified_market_snapshot",
    "upstream_tool_get_fundamentals", "upstream_tool_get_balance_sheet", "upstream_tool_get_cashflow",
    "upstream_tool_get_income_statement", "upstream_tool_get_news", "upstream_tool_get_global_news",
    "upstream_tool_get_insider_transactions", "upstream_tool_get_macro_indicators", "upstream_tool_get_prediction_markets",
}


@traced("india.langgraph.swing_analysis")
def run_existing_graph(symbol: str, trade_date: str, analysts: tuple[str, ...] = ("market", "news", "fundamentals"),
                       *, run_id: str | None = None, explicit_user_request: bool = False,
                       external_context: str | None = None) -> dict[str, Any]:
    # A direct dashboard action is explicit consent to run the configured LLM graph.
    # Non-interactive/background callers must opt in through the environment flag.
    if not explicit_user_request and os.getenv("ENABLE_LANGGRAPH_AGENT_ANALYSIS", "false").lower() not in {"1", "true", "yes"}:
        raise AgentRunDisabled("Full LangGraph analysis is disabled for non-interactive callers. Set ENABLE_LANGGRAPH_AGENT_ANALYSIS=true after configuring an LLM provider and reviewing its cost/data behavior.")
    try:
        parsed_date = date.fromisoformat(trade_date)
    except ValueError as exc:
        raise ValueError("trade_date must be YYYY-MM-DD") from exc
    if parsed_date.isoformat() != trade_date:
        raise ValueError("trade_date must be YYYY-MM-DD")
    ticker = symbol.upper()
    if not ticker.endswith(".NS"):
        ticker = f"{ticker}.NS"
    # Imports are delayed so the dashboard can start without constructing LLM clients.
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph(selected_analysts=analysts, debug=False, config=DEFAULT_CONFIG.copy())
    try:
        package_candidate = external_context
        if isinstance(package_candidate, str):
            try:
                import json
                package_candidate = json.loads(package_candidate)
            except (TypeError, ValueError):
                package_candidate = None
        callbacks = [_make_tool_audit_callback(run_id, india_adapter_active=is_india_evidence_package(package_candidate))] if run_id else None
        # Scope the shared India evidence to this graph invocation. Original analyst
        # tools route through India adapters during this scope and restore their
        # upstream provider behavior immediately after the run.
        with use_india_evidence(external_context):
            state, decision = graph.propagate(ticker, trade_date, asset_type="stock",
                                              initial_context=external_context, callbacks=callbacks)
        report_path = None
        try:
            report_path = str(graph.save_reports(state, ticker))
        except Exception as exc:  # report persistence must not erase a completed decision
            logger.warning("LangGraph decision completed but report saving failed: %s", type(exc).__name__)
        # Keep response small and JSON-safe; detailed reports are saved by the upstream framework.
        summary_keys = ("market_report", "sentiment_report", "news_report", "fundamentals_report", "investment_plan", "trader_investment_plan", "final_trade_decision")
        summaries = {key: str(state[key])[:16000] for key in summary_keys if isinstance(state, dict) and state.get(key)}
        debate = state.get("investment_debate_state", {}) if isinstance(state, dict) else {}
        risk = state.get("risk_debate_state", {}) if isinstance(state, dict) else {}
        if debate:
            summaries["bull_bear_debate"] = ("BULL CASE\n" + str(debate.get("bull_history", "")) +
                                               "\n\nBEAR CASE\n" + str(debate.get("bear_history", "")))[:16000]
        if risk:
            summaries["risk_debate"] = ("AGGRESSIVE\n" + str(risk.get("aggressive_history", "")) +
                                          "\n\nCONSERVATIVE\n" + str(risk.get("conservative_history", "")) +
                                          "\n\nNEUTRAL\n" + str(risk.get("neutral_history", "")))[:16000]
        # Final-state statuses are a fallback for nodes that did not emit a
        # recognizable live callback; the API reconciler preserves observed callbacks.
        def stage(component_id: str, name: str, evidence: Any, purpose: str) -> dict[str, Any]:
            present = bool(str(evidence or "").strip())
            return {"component_id": component_id, "name": name,
                    "status": "completed" if present else "unknown",
                    "reason": None if present else "No output was present in the completed graph state; exact reachability is unknown.",
                    "details": {"output_present": present, "purpose": purpose,
                                "output_summary": str(evidence or "")[:1200],
                                "status_basis": "completed_graph_state"}}
        social_stage = (
            stage("upstream_social_analyst", "Sentiment Analyst", state.get("sentiment_report"), "Review available social sentiment evidence.")
            if "social" in analysts else
            {"component_id": "upstream_social_analyst", "name": "Sentiment Analyst", "status": "skipped",
             "reason": "Social/sentiment analyst was not selected for this run.",
             "details": {"selected_analysts": list(analysts), "status_basis": "run_configuration"}}
        )
        stages = [
            stage("upstream_market_analyst", "Technical / Market Analyst", state.get("market_report"), "Produce market and technical report."),
            stage("upstream_fundamentals_analyst", "Fundamental Analyst", state.get("fundamentals_report"), "Review financial statements and fundamentals."),
            stage("upstream_news_analyst", "News Analyst", state.get("news_report"), "Review company and macro news."),
            social_stage,
            stage("bull_researcher", "Bullish Researcher", debate.get("bull_history"), "Build the bullish thesis."),
            stage("bear_researcher", "Bearish Researcher", debate.get("bear_history"), "Challenge the bullish thesis with bearish evidence."),
            stage("research_manager", "Research Manager", state.get("investment_plan"), "Resolve bull/bear debate into an investment plan."),
            stage("trader_agent", "Trader", state.get("trader_investment_plan"), "Translate the investment plan into a proposed trade."),
            stage("aggressive_analyst", "Aggressive Risk Analyst", risk.get("aggressive_history"), "Challenge the plan from an aggressive risk perspective."),
            stage("conservative_analyst", "Conservative Risk Analyst", risk.get("conservative_history"), "Challenge the plan from a conservative risk perspective."),
            stage("neutral_analyst", "Neutral Risk Analyst", risk.get("neutral_history"), "Assess the plan from a neutral risk perspective."),
            stage("portfolio_manager", "Hedge Fund / Portfolio Manager", state.get("final_trade_decision"), "Synthesize the debates into the final portfolio decision."),
        ]
        return {"symbol": ticker, "trade_date": trade_date, "decision": str(decision),
                "selected_analysts": list(analysts), "summaries": summaries, "stages": stages,
                "report_path": report_path, "run_id": run_id, "live_trading_enabled": False,
                "data_note": "The main UI fetches FYERS technical data and point-in-time local facts/events first, then injects a compact evidence snapshot into the upstream graph's shared analyst context. The integrated full-analysis path selects all four original analysts (market, social/sentiment, news and fundamentals), followed by Bull/Bear research, Research Manager, Trader, all three risk analysts and Portfolio Manager. The graph also uses its configured analyst dataflows. Analysts should flag disagreements between supplied evidence and tool results rather than silently replacing either source."}
    finally:
        # The graph has no long-running worker process; releasing local resources is best-effort.
        close = getattr(graph, "close", None)
        if callable(close):
            close()

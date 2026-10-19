"""Compiled LangGraph orchestration for the new India trading-analysis pipeline.

The graph is research-only: it never places orders. Provider adapters and LLM
interpreters are injectable so graph behavior can be tested without credentials.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

from tradingagents.india.langgraph_pipeline.contracts import validate_final_verdict, validate_request
from tradingagents.india.langgraph_pipeline.data_collection import EvidenceCollector, collect_evidence
from tradingagents.india.langgraph_pipeline.agents.fundamental_analyst.agent import analyze_fundamentals
from tradingagents.india.langgraph_pipeline.agents.technical_analyst.agent import analyze_technicals
from tradingagents.india.langgraph_pipeline.agents.trader.agent import build_trade_proposal
from tradingagents.india.langgraph_pipeline.agents.risk_analyst.agent import review_trade_risk
from tradingagents.india.langgraph_pipeline.agents.hedge_fund_manager.agent import build_manager_verdict

Node = Callable[[dict[str, Any]], dict[str, Any]]


def _trace(state: dict[str, Any], name: str) -> list[str]:
    previous = state.get("workflow_trace", [])
    return [*previous, name] if isinstance(previous, list) else [name]


def _error(state: dict[str, Any], node: str, exc: Exception, *, report_key: str | None = None) -> dict[str, Any]:
    errors = list(state.get("errors", [])) if isinstance(state.get("errors"), list) else []
    errors.append({"node": node, "type": type(exc).__name__, "message": str(exc), "at": datetime.now().astimezone().isoformat()})
    update: dict[str, Any] = {"errors": errors, "workflow_trace": _trace(state, node)}
    if report_key:
        update[report_key] = {
            "status": "error", "summary": f"{node} failed; no unsupported conclusion was generated.",
            "findings": [], "risks": [], "evidence_refs": [], "confidence": 0.0,
            "missing_data": [f"{node} execution failed"], "error": str(exc),
        }
    return update


def _validate_request_node(state: dict[str, Any]) -> dict[str, Any]:
    request = validate_request(state.get("request", {}))
    return {"request": request, "run_id": state.get("run_id") or str(uuid4()), "status": "running",
            "workflow_trace": _trace(state, "validate_request"), "errors": list(state.get("errors", []))}


def _collection_node(state: dict[str, Any], collector: EvidenceCollector | None, search_missing: bool,
                     max_attempts: int) -> dict[str, Any]:
    """Retry exceptions a bounded number of times; provider-reported errors remain visible."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            update = collect_evidence(state, collector=collector, search_missing=search_missing)
            update["workflow_trace"] = [*_trace(state, "collect_evidence"), f"collection_attempt:{attempt}"]
            update["collection_attempts"] = attempt
            return update
        except Exception as exc:  # bounded retry at external data boundary
            last_error = exc
    assert last_error is not None
    update = _error(state, "collect_evidence", last_error)
    update.update({"status": "failed", "market_data": {"candles_by_timeframe": {}},
                   "fundamental_data": {"facts": []}, "news_data": [], "events_data": [],
                   "evidence_metadata": {"collection_failed": True}, "collection_attempts": max_attempts})
    return update


def _fundamental_node(state: dict[str, Any], interpreter: Callable[..., Any] | None) -> dict[str, Any]:
    try:
        update = analyze_fundamentals(state, interpreter=interpreter)
        update["workflow_trace"] = _trace(state, "fundamental_analyst")
        return update
    except Exception as exc:
        return _error(state, "fundamental_analyst", exc, report_key="fundamental_analysis")


def _technical_node(state: dict[str, Any], interpreter: Callable[..., Any] | None) -> dict[str, Any]:
    try:
        update = analyze_technicals(state, interpreter=interpreter)
        update["workflow_trace"] = _trace(state, "technical_analyst")
        return update
    except Exception as exc:
        return _error(state, "technical_analyst", exc, report_key="technical_analysis")


def _trader_node(state: dict[str, Any], interpreter: Callable[..., Any] | None, minimum_reward_risk: float) -> dict[str, Any]:
    try:
        update = build_trade_proposal(state, interpreter=interpreter, minimum_reward_risk=minimum_reward_risk)
        update["workflow_trace"] = _trace(state, "trader_agent")
        return update
    except Exception as exc:
        failure = _error(state, "trader_agent", exc)
        failure["trader_analysis"] = {"action": "NEEDS_REVIEW", "direction": None, "entry": None,
            "stop_loss": None, "target": None, "reward_risk_ratio": None,
            "rationale": ["Trader Agent failed; manual review is required."], "missing_data": [str(exc)]}
        return failure


def _risk_node(state: dict[str, Any]) -> dict[str, Any]:
    try:
        update = review_trade_risk(state)
        update["workflow_trace"] = _trace(state, "risk_analyst")
        return update
    except Exception as exc:
        failure = _error(state, "risk_analyst", exc)
        failure["risk_analysis"] = {"status": "needs_review", "checks": [], "hard_blocks": [],
            "warnings": [], "needs_review": ["Risk analysis failed; trade cannot be approved."],
            "error": str(exc), "live_order_placed": False}
        return failure


def _manager_node(state: dict[str, Any], interpreter: Callable[..., Any] | None) -> dict[str, Any]:
    try:
        update = build_manager_verdict(state, interpreter=interpreter)
        update["workflow_trace"] = _trace(state, "hedge_fund_manager")
        return update
    except Exception as exc:
        failure = _error(state, "hedge_fund_manager", exc, report_key="manager_analysis")
        failure["final_verdict"] = {"verdict": "NEEDS_REVIEW", "rationale": ["Final reconciliation failed."],
            "supporting_evidence": [], "contradicting_evidence": [], "risk_status": "needs_review",
            "confidence": 0.0, "data_quality": "insufficient", "live_order_placed": False}
        return failure


def _risk_rejection_node(state: dict[str, Any]) -> dict[str, Any]:
    risk = state.get("risk_analysis") or {}
    blocks = risk.get("hard_blocks", []) if isinstance(risk, dict) else []
    rationale = list(blocks) if isinstance(blocks, list) else []
    if not rationale:
        rationale = ["Risk Analyst rejected the proposed trade."]
    verdict = {"verdict": "NO_TRADE", "rationale": rationale,
        "supporting_evidence": [], "contradicting_evidence": [], "risk_status": "rejected",
        "confidence": 0.0, "data_quality": "insufficient", "live_order_placed": False}
    return {"final_verdict": validate_final_verdict(verdict), "workflow_trace": _trace(state, "risk_rejection")}


def _route_after_risk(state: dict[str, Any]) -> str:
    risk = state.get("risk_analysis") or {}
    if isinstance(risk, dict) and (risk.get("status") == "rejected" or bool(risk.get("hard_blocks"))):
        return "risk_rejection"
    return "hedge_fund_manager"


def _validate_final_node(state: dict[str, Any]) -> dict[str, Any]:
    try:
        verdict = validate_final_verdict(state.get("final_verdict", {}))
        status = "completed" if verdict["data_quality"] == "good" else "partial"
        return {"final_verdict": verdict, "status": status, "workflow_trace": _trace(state, "validate_final_verdict")}
    except Exception as exc:
        errors = list(state.get("errors", []))
        errors.append({"node": "validate_final_verdict", "type": type(exc).__name__, "message": str(exc)})
        safe = validate_final_verdict({"verdict": "NEEDS_REVIEW", "rationale": ["Final verdict failed validation."],
            "supporting_evidence": [], "contradicting_evidence": [], "risk_status": "needs_review",
            "confidence": 0.0, "data_quality": "insufficient", "live_order_placed": False})
        return {"final_verdict": safe, "status": "partial", "errors": errors,
                "workflow_trace": _trace(state, "validate_final_verdict")}


def create_trading_analysis_graph(
    *,
    collector: EvidenceCollector | None = None,
    search_missing: bool = False,
    fundamental_interpreter: Callable[..., Any] | None = None,
    technical_interpreter: Callable[..., Any] | None = None,
    trader_interpreter: Callable[..., Any] | None = None,
    manager_interpreter: Callable[..., Any] | None = None,
    minimum_reward_risk: float = 1.5,
    collection_max_attempts: int = 2,
    checkpointer: Any | None = None,
) -> Any:
    """Build and compile the graph. Requires the optional ``langgraph`` package.

    The graph accepts an initial state with ``request`` and optionally ``portfolio``
    and ``risk_limits``. Inject a collector/interpreters for tests or deployment.
    """
    if collection_max_attempts < 1:
        raise ValueError("collection_max_attempts must be >= 1")
    if minimum_reward_risk <= 0:
        raise ValueError("minimum_reward_risk must be > 0")
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise RuntimeError("LangGraph is required to compile this workflow. Install project dependencies first.") from exc

    from tradingagents.india.langgraph_pipeline.state import TradingAnalysisState

    builder = StateGraph(TradingAnalysisState)
    builder.add_node("validate_request", _validate_request_node)
    builder.add_node("collect_evidence", lambda s: _collection_node(s, collector, search_missing, collection_max_attempts))
    builder.add_node("fundamental_analyst", lambda s: _fundamental_node(s, fundamental_interpreter))
    builder.add_node("technical_analyst", lambda s: _technical_node(s, technical_interpreter))
    builder.add_node("trader_agent", lambda s: _trader_node(s, trader_interpreter, minimum_reward_risk))
    builder.add_node("risk_analyst", _risk_node)
    builder.add_node("risk_rejection", _risk_rejection_node)
    builder.add_node("hedge_fund_manager", lambda s: _manager_node(s, manager_interpreter))
    builder.add_node("validate_final_verdict", _validate_final_node)

    builder.add_edge(START, "validate_request")
    builder.add_edge("validate_request", "collect_evidence")
    # Run specialists sequentially so trace/error state remains deterministic;
    # they are independently callable and can be parallelized later with reducers.
    builder.add_edge("collect_evidence", "fundamental_analyst")
    builder.add_edge("fundamental_analyst", "technical_analyst")
    builder.add_edge("technical_analyst", "trader_agent")
    builder.add_edge("trader_agent", "risk_analyst")
    builder.add_conditional_edges("risk_analyst", _route_after_risk,
                                  {"risk_rejection": "risk_rejection", "hedge_fund_manager": "hedge_fund_manager"})
    builder.add_edge("risk_rejection", "validate_final_verdict")
    builder.add_edge("hedge_fund_manager", "validate_final_verdict")
    builder.add_edge("validate_final_verdict", END)
    return builder.compile(checkpointer=checkpointer)


def run_trading_analysis(request: dict[str, Any], **graph_options: Any) -> dict[str, Any]:
    """Convenience entry point for a single analysis request."""
    graph_keys = {key: graph_options.pop(key) for key in list(graph_options) if key in {
        "collector", "search_missing", "fundamental_interpreter", "technical_interpreter",
        "trader_interpreter", "manager_interpreter", "minimum_reward_risk",
        "collection_max_attempts", "checkpointer"}}
    graph = create_trading_analysis_graph(**graph_keys)
    initial: dict[str, Any] = {"request": request, "status": "initialized", "errors": [], "workflow_trace": []}
    for key in ("portfolio", "risk_limits", "run_id"):
        if key in graph_options:
            initial[key] = deepcopy(graph_options[key])
    config = graph_options.get("config")
    return graph.invoke(initial, config=config) if config is not None else graph.invoke(initial)

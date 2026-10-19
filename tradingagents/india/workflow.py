"""Integrated LangGraph workflow over validated India technical, event and fundamental evidence."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict

from tradingagents.india.tracing import traced
from tradingagents.india.execution_audit import mark_component
from tradingagents.india.market_regime import analyze_market_regime
from tradingagents.india.catalyst_analysis import analyze_catalysts
from tradingagents.india.portfolio_risk import assess_portfolio_risk


class IndiaResearchState(TypedDict, total=False):
    symbol: str
    as_of: str
    technical: dict[str, Any]
    events_input: list[dict[str, Any]]
    facts_input: list[dict[str, Any]]
    calendar_events_input: list[dict[str, Any]]
    market_context_input: dict[str, Any]
    financial_health_flags_input: dict[str, Any]
    fundamental_analysis_input: dict[str, Any]
    portfolio_input: dict[str, Any] | None
    portfolio_risk_limits_input: dict[str, Any]
    use_llm: bool
    evidence: dict[str, Any]
    risk_flags: list[dict[str, Any]]
    assessment: dict[str, Any]
    llm_summary: str
    llm_used: bool
    workflow: list[str]


def _parse_published(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normalize_evidence(state: IndiaResearchState) -> dict[str, Any]:
    cutoff = datetime.fromisoformat(state["as_of"].replace("Z", "+00:00"))
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("as_of must include a timezone offset")
    cutoff = cutoff.astimezone(timezone.utc)
    events = []
    excluded_events = 0
    for event in state.get("events_input", []):
        published = _parse_published(event.get("published_at"))
        if published is None or published > cutoff:
            excluded_events += 1
            continue
        events.append({**event, "published_at_utc": published.isoformat()})
    facts = []
    excluded_facts = 0
    for fact in state.get("facts_input", []):
        published = _parse_published(fact.get("published_at"))
        if published is None or published > cutoff:
            excluded_facts += 1
            continue
        facts.append({**fact, "published_at_utc": published.isoformat()})
    events.sort(key=lambda e: e["published_at_utc"], reverse=True)
    facts.sort(key=lambda f: (f.get("period_end", ""), f["published_at_utc"]), reverse=True)
    calendar_events = [row for row in state.get("calendar_events_input", []) if isinstance(row, dict)]
    market_context = state.get("market_context_input") if isinstance(state.get("market_context_input"), dict) else {"status": "unavailable"}
    return {"evidence": {"events": events[:50], "calendar_events": calendar_events[:100], "facts": facts[:200],
                         "market_context": market_context,
                         "financial_health_flags": state.get("financial_health_flags_input") or {},
                         "fundamental_analysis": state.get("fundamental_analysis_input") or {},
                         "excluded_events_unknown_or_future_timestamp": excluded_events,
                         "excluded_facts_unknown_or_future_timestamp": excluded_facts,
                         "as_of": cutoff.isoformat()},
            "workflow": ["normalize_point_in_time_evidence"]}


def _assess_risk(state: IndiaResearchState) -> dict[str, Any]:
    evidence = state.get("evidence", {})
    technical = state.get("technical", {})
    flags: list[dict[str, Any]] = []
    bias = technical.get("multi_timeframe_bias", "insufficient_data")
    if bias == "bullish_alignment":
        flags.append({"severity": "positive_setup_context", "reason": "Daily, 4H and 1H trends are aligned bullish; this is not an order signal."})
    elif bias == "bearish_alignment":
        flags.append({"severity": "high", "reason": "Daily, 4H and 1H trends are aligned bearish; bullish swing entries may be less favorable."})
    elif bias in {"mixed_bullish_bias", "mixed_bearish_bias", "mixed_or_neutral"}:
        flags.append({"severity": "medium", "reason": f"Timeframes are not fully aligned ({bias})."})
    events = evidence.get("events", [])
    for event in events:
        if event.get("severity") == "high" and event.get("direction") in {"negative", "mixed", "uncertain"}:
            flags.append({"severity": "high", "type": "material_event", "title": event.get("title"),
                          "event_type": event.get("event_type"), "direction": event.get("direction"),
                          "source_url": event.get("source_url"), "published_at": event.get("published_at")})
    calendar_events = evidence.get("calendar_events", [])
    try:
        as_of_day = datetime.fromisoformat(str(state.get("as_of", "")).replace("Z", "+00:00")).date()
    except ValueError:
        as_of_day = None
    facts = evidence.get("facts", [])
    for fact in facts:
        qoq = fact.get("qoq")
        if not isinstance(qoq, dict):
            continue
        pct = qoq.get("percentage_change")
        metric = str(fact.get("metric_code", "")).casefold()
        if isinstance(pct, (int, float)) and pct <= -5 and any(term in metric for term in ("profit", "revenue", "income", "nii", "net_interest")):
            flags.append({"severity": "high", "type": "fundamental_decline", "metric_code": metric,
                          "qoq_percentage_change": pct, "period_end": fact.get("period_end"),
                          "source_url": fact.get("source_url")})
    market_context = evidence.get("market_context", {})
    market_regime = analyze_market_regime(market_context)
    catalysts = analyze_catalysts(calendar_events=calendar_events, news_events=events,
                                  as_of=str(state.get("as_of", "")), symbol=state.get("symbol"))
    for catalyst in catalysts.get("upcoming_catalysts", []):
        if catalyst.get("importance") in {"high", "critical"} or catalyst.get("corporate_catalyst"):
            flags.append({"severity": "high" if catalyst.get("verified") and catalyst.get("importance") in {"high", "critical"} else "medium",
                          "type": "corporate_catalyst", "title": catalyst.get("title"),
                          "event_date": catalyst.get("event_date"), "days_from_as_of": catalyst.get("days_from_as_of"),
                          "verified": catalyst.get("verified"), "category": catalyst.get("category"),
                          "source_url": catalyst.get("source_url"),
                          "reason": "Upcoming corporate catalyst may increase gap/volatility risk; direction is not inferred."})
    if market_regime.get("regime") == "risk_off_breadth":
        flags.append({"severity": "medium", "type": "market_regime", "reason": "Negative breadth across the configured scanner subset; bullish setups need stronger confirmation.", "regime": market_regime.get("regime"), "coverage_count": market_regime.get("coverage_count")})
    elif market_regime.get("regime") == "risk_on_breadth":
        flags.append({"severity": "positive_setup_context", "type": "market_regime", "reason": "Positive breadth across the configured scanner subset; this is supportive context, not a trade signal.", "regime": market_regime.get("regime"), "coverage_count": market_regime.get("coverage_count")})
    if market_regime.get("alignment") == "divergent":
        flags.append({"severity": "medium", "type": "stock_market_divergence", "reason": "The selected stock trend conflicts with broader scanner breadth.", "selected_stock_bias": market_regime.get("selected_stock_bias"), "regime": market_regime.get("regime")})
    # Surface sourced statement-health flags as risk evidence. Do not convert
    # absence of a metric into a negative or positive conclusion.
    financial_health_flags = evidence.get("financial_health_flags") or {}
    fundamental_red_flags = financial_health_flags.get("red_flags", []) if isinstance(financial_health_flags, dict) else []
    fundamental_green_flags = financial_health_flags.get("green_flags", []) if isinstance(financial_health_flags, dict) else []
    for flag in fundamental_red_flags:
        if isinstance(flag, dict):
            flags.append({"severity": "high" if flag.get("severity") == "red" else "medium",
                          "type": "fundamental_health_flag", "code": flag.get("code"),
                          "title": flag.get("title"), "detail": flag.get("detail"),
                          "periods": flag.get("periods", [])})
    technical_price = None
    for price_key in ("current_price", "close", "last_price", "price"):
        candidate = technical.get(price_key)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            technical_price = candidate
            break
    portfolio_risk = assess_portfolio_risk(
        symbol=str(state.get("symbol", "")),
        portfolio=state.get("portfolio_input"),
        current_price=technical_price,
        limits=state.get("portfolio_risk_limits_input") or {},
    )
    for check in portfolio_risk.get("checks", []):
        if check.get("status") == "breach":
            flags.append({"severity": "high", "type": "portfolio_limit_breach", **check,
                          "reason": "A caller-configured portfolio exposure limit was exceeded."})
    eligible_calendar_count = len(catalysts.get("upcoming_catalysts", [])) + len(catalysts.get("recent_catalysts", []))
    assessment = {"technical_bias": bias, "portfolio_risk": portfolio_risk, "event_count_used": len(events),
                  "calendar_event_count_used": eligible_calendar_count, "calendar_event_count_received": len(calendar_events),
                  "financial_facts_used": len(facts),
                  "fundamental_health_red_flags": len(fundamental_red_flags),
                  "fundamental_health_green_flags": len(fundamental_green_flags),
                  "fundamental_health_status": "available" if financial_health_flags else "unavailable",
                  "market_context_status": market_context.get("status", "unavailable"),
                  "market_breadth": market_context.get("market_breadth"),
                  "market_regime": market_regime, "corporate_catalysts": catalysts,
                  "selected_stock_market_snapshot": market_context.get("selected_stock"),
                  "risk_flag_count": len(flags), "risk_flags": flags,
                  "data_quality_warnings": [
                      f"{evidence.get('excluded_events_unknown_or_future_timestamp', 0)} events excluded because publication time was missing or after as_of.",
                      f"{evidence.get('excluded_facts_unknown_or_future_timestamp', 0)} financial facts excluded because publication time was missing or after as_of.",
                      f"{catalysts.get('excluded_future_known_at', 0)} calendar events excluded because known-at time was after as_of.",
                      f"{catalysts.get('excluded_unknown_known_at', 0)} calendar events excluded because known-at time was missing or invalid.",
                      f"{catalysts.get('excluded_other_symbol', 0)} calendar events excluded because they explicitly belonged to another symbol.",
                  ],
                  "decision": "review_required" if flags or bias not in {"bullish_alignment", "bearish_alignment"} else "evidence_aligned_review_required",
                  "live_order_placed": False}
    return {"risk_flags": flags, "assessment": assessment, "workflow": [*state.get("workflow", []), "deterministic_event_and_fundamental_risk_checks"]}


def _synthesize(state: IndiaResearchState) -> dict[str, Any]:
    evidence = state.get("evidence", {})
    assessment = state.get("assessment", {})
    deterministic = (
        f"Research summary for {state.get('symbol')}: technical bias={assessment.get('technical_bias')}; "
        f"news/events used={assessment.get('event_count_used')}; scheduled calendar events used={assessment.get('calendar_event_count_used', 0)}; financial facts used={assessment.get('financial_facts_used')}; "
        f"market regime={assessment.get('market_regime', {}).get('regime', 'unknown')}; "
        f"market/stock alignment={assessment.get('market_regime', {}).get('alignment', 'unknown')}; "
        f"upcoming corporate catalysts={assessment.get('corporate_catalysts', {}).get('material_upcoming_count', 0)}; "
        f"risk flags={assessment.get('risk_flag_count')}. This output is evidence triage, not a buy/sell instruction. "
        "Review source links, data freshness, liquidity, market conditions and risk limits before acting."
    )
    if not state.get("use_llm"):
        return {"llm_summary": deterministic, "llm_used": False,
                "workflow": [*state.get("workflow", []), "deterministic_summary"]}
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.llm_clients import create_tier_client

        config = DEFAULT_CONFIG.copy()
        llm = create_tier_client(config, "quick").get_llm()
        context = {"symbol": state.get("symbol"), "as_of": state.get("as_of"),
                   "technical": state.get("technical"), "assessment": assessment,
                   "events": evidence.get("events", [])[:15],
                   "calendar_events": evidence.get("calendar_events", [])[:30],
                   "market_context": evidence.get("market_context", {}),
                   "corporate_catalysts": assessment.get("corporate_catalysts", {}),
                   "financial_facts": evidence.get("facts", [])[:40],
                   "financial_health_flags": evidence.get("financial_health_flags"),
                   "fundamental_analysis": evidence.get("fundamental_analysis", {}),
                   "portfolio_risk": assessment.get("portfolio_risk", {})}
        prompt = (
            "You are an evidence-grounded Indian equity swing-research analyst. Summarize only the supplied data. "
            "Do not invent prices, financial values, dates, news, or sources. Distinguish facts from interpretation. "
            "Do not give a direct buy/sell instruction or claim profitability. Identify conflicting signals, source gaps, "
            "potentially material events and corporate catalysts, their source quality, and what would invalidate the thesis. If evidence is insufficient, say so. "
            "Cite source URLs from the supplied evidence.\n\nDATA:\n" + json.dumps(context, ensure_ascii=False, default=str)[:24000]
        )
        response = llm.invoke(prompt)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "\n".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
        return {"llm_summary": str(content)[:16000], "llm_used": True,
                "workflow": [*state.get("workflow", []), "llm_evidence_synthesis"]}
    except Exception as exc:
        return {"llm_summary": deterministic + f" LLM synthesis was unavailable ({type(exc).__name__}); deterministic summary returned.",
                "llm_used": False, "llm_error_type": type(exc).__name__,
                "workflow": [*state.get("workflow", []), "deterministic_fallback_after_llm_error"]}


@traced("india.langgraph.integrated_research")
def run_integrated_research(*, symbol: str, as_of: str, technical: dict[str, Any],
                            events: list[dict[str, Any]], facts: list[dict[str, Any]],
                            calendar_events: list[dict[str, Any]] | None = None,
                            market_context: dict[str, Any] | None = None,
                            financial_health_flags: dict[str, Any] | None = None,
                            fundamental_analysis: dict[str, Any] | None = None,
                            portfolio: dict[str, Any] | None = None,
                            portfolio_risk_limits: dict[str, Any] | None = None,
                            use_llm: bool = False, run_id: str | None = None) -> dict[str, Any]:
    """Run a LangGraph over technical, news/event and financial evidence."""
    initial: IndiaResearchState = {"symbol": symbol, "as_of": as_of, "technical": technical,
                                  "events_input": events, "facts_input": facts,
                                  "calendar_events_input": calendar_events or [],
                                  "market_context_input": market_context or {"status": "unavailable"},
                                  "financial_health_flags_input": financial_health_flags or {},
                                  "fundamental_analysis_input": fundamental_analysis or {},
                                  "portfolio_input": portfolio,
                                  "portfolio_risk_limits_input": portfolio_risk_limits or {},
                                  "use_llm": use_llm}
    def tracked(component_id: str, fn):
        def invoke_node(state):
            if run_id:
                mark_component(run_id, component_id, "running", event_type="node_started", message=f"Node {component_id} started")
            try:
                output = fn(state)
                if run_id:
                    mark_component(run_id, component_id, "completed", details={"output_keys": sorted(output.keys()), "output_summary": str(output)[:1200]}, event_type="node_completed", message=f"Node {component_id} completed")
                return output
            except Exception as exc:
                if run_id:
                    mark_component(run_id, component_id, "failed", reason=f"Node raised {type(exc).__name__}.", details={"error_type": type(exc).__name__}, event_type="node_failed", message=f"Node {component_id} failed")
                raise
        return invoke_node

    try:
        from langgraph.graph import END, START, StateGraph
        graph = StateGraph(IndiaResearchState)
        graph.add_node("normalize_evidence", tracked("normalize_evidence", _normalize_evidence))
        graph.add_node("assess_risk", tracked("risk_assessment", _assess_risk))
        graph.add_node("synthesize", tracked("synthesis", _synthesize))
        graph.add_edge(START, "normalize_evidence")
        graph.add_edge("normalize_evidence", "assess_risk")
        graph.add_edge("assess_risk", "synthesize")
        graph.add_edge("synthesize", END)
        result = graph.compile().invoke(initial)
        engine = "langgraph"
    except ImportError:
        # Development/test fallback; the launch script installs the project dependencies, including LangGraph.
        result = dict(initial)
        result.update(tracked("normalize_evidence", _normalize_evidence)(result))
        result.update(tracked("risk_assessment", _assess_risk)(result))
        result.update(tracked("synthesis", _synthesize)(result))
        result["workflow"] = [*result.get("workflow", []), "sequential_fallback_langgraph_dependency_missing"]
        engine = "sequential_fallback"
    return {"symbol": symbol, "as_of": as_of, "technical": technical,
            "evidence": result.get("evidence", {}), "assessment": result.get("assessment", {}),
            "market_regime": result.get("assessment", {}).get("market_regime", {}),
            "portfolio_risk": result.get("assessment", {}).get("portfolio_risk", {}),
            "summary": result.get("llm_summary", ""), "llm_used": result.get("llm_used", False),
            "workflow": result.get("workflow", []), "workflow_engine": engine,
            "note": "Integrated research output only; not an order or financial advice."}

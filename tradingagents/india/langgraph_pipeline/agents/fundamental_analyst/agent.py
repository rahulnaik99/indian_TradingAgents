"""Fundamental analyst node for the new India trading-analysis graph.

Financial comparisons are calculated by the existing deterministic fundamentals
module. An optional interpreter can summarize the sourced facts, but cannot
supply missing numbers. No network access occurs unless the caller explicitly
provides an interpreter that performs it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from tradingagents.india.fundamentals import summarize_facts


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None and result.utcoffset() is not None else None


def _fact_refs(facts: list[dict[str, Any]]) -> list[str]:
    refs = []
    for fact in facts:
        source = fact.get("source_url") or fact.get("source_name")
        metric = fact.get("metric_code", "unknown_metric")
        period = fact.get("period_end", "unknown_period")
        if source:
            refs.append(f"{metric}:{period}:{source}")
    return list(dict.fromkeys(refs))


def analyze_fundamentals(state: dict[str, Any], *, interpreter: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """LangGraph-compatible Fundamental Analyst node.

    Input is the shared state with ``request`` and ``fundamental_data.facts``.
    Output updates only ``fundamental_analysis`` and ``workflow_trace``.
    ``interpreter`` is an optional injectable LLM/application function; it receives
    calculated evidence and must return a dict. Deterministic findings remain the
    source of truth and are not overwritten by interpreter output.
    """
    request = state.get("request")
    if not isinstance(request, dict):
        raise ValueError("state.request is required")
    symbol = request.get("symbol")
    cutoff = _parse_timestamp(request.get("as_of"))
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("request.symbol is required")
    if cutoff is None:
        raise ValueError("request.as_of must be timezone-aware")

    payload = state.get("fundamental_data") or {}
    raw_facts = payload.get("facts", []) if isinstance(payload, dict) else []
    if not isinstance(raw_facts, list):
        raw_facts = []

    # Defensive point-in-time filtering is repeated at the analyst boundary.
    visible_facts: list[dict[str, Any]] = []
    excluded_undated = 0
    excluded_future = 0
    for fact in raw_facts:
        if not isinstance(fact, dict):
            continue
        published = _parse_timestamp(fact.get("published_at"))
        if published is None:
            excluded_undated += 1
            continue
        if published > cutoff:
            excluded_future += 1
            continue
        if str(fact.get("symbol", symbol)).upper() != symbol.upper():
            continue
        visible_facts.append(fact)

    summary = summarize_facts(visible_facts, as_of=cutoff)
    metric_codes = {str(f.get("metric_code", "")).upper() for f in visible_facts}
    findings: list[dict[str, Any]] = []
    for fact in summary.get("facts", []):
        metric = str(fact.get("metric_code", "")).upper()
        if metric in {"REVENUE", "OPERATING_REVENUE", "NET_INCOME", "EBITDA", "OPERATING_CASH_FLOW", "FREE_CASH_FLOW", "TOTAL_DEBT", "NET_DEBT", "CASH_AND_EQUIVALENTS", "TOTAL_ASSETS", "TOTAL_LIABILITIES", "SHAREHOLDERS_EQUITY", "EPS_BASIC", "EPS_DILUTED", "EPS", "PE_RATIO", "FORWARD_PE", "PB_RATIO", "ROE", "DEBT_TO_EQUITY", "DIVIDEND_YIELD", "MARKET_CAP"}:
            findings.append({
                "type": "financial_metric",
                "metric_code": metric,
                "value": fact.get("value"),
                "unit": fact.get("unit"),
                "currency": fact.get("currency"),
                "scope": fact.get("scope"),
                "period_type": fact.get("period_type"),
                "period_end": fact.get("period_end"),
                "published_at": fact.get("published_at"),
                "qoq": fact.get("qoq"),
                "yoy": fact.get("yoy"),
                "source_name": fact.get("source_name"),
                "source_url": fact.get("source_url"),
            })

    missing = []
    for code, label in (("REVENUE", "revenue"), ("NET_INCOME", "net income"), ("OPERATING_CASH_FLOW", "operating cash flow"), ("TOTAL_DEBT", "total debt")):
        if code not in metric_codes:
            missing.append(label)

    risks: list[dict[str, Any]] = []
    for fact in visible_facts:
        metric = str(fact.get("metric_code", "")).upper()
        try:
            value = float(fact.get("value"))
        except (TypeError, ValueError):
            continue
        if metric in {"NET_INCOME", "OPERATING_CASH_FLOW", "FREE_CASH_FLOW"} and value < 0:
            risks.append({"type": "negative_financial_metric", "metric_code": metric,
                          "value": value, "period_end": fact.get("period_end"),
                          "source_url": fact.get("source_url")})

    status = "ok" if visible_facts and not missing else ("limited" if visible_facts else "unavailable")
    report: dict[str, Any] = {
        "status": status,
        "summary": (f"Found {len(visible_facts)} timestamped financial facts for {symbol.upper()} as of {cutoff.isoformat()}."
                    if visible_facts else f"No eligible, timestamped financial facts were available for {symbol.upper()} at the requested cutoff."),
        "findings": findings,
        "risks": risks,
        "evidence_refs": _fact_refs(visible_facts),
        "confidence": 0.65 if visible_facts and not missing else (0.35 if visible_facts else 0.0),
        "missing_data": missing,
        "data_quality": {"facts_used": len(visible_facts), "excluded_undated": excluded_undated,
                         "excluded_future": excluded_future, "metric_codes": sorted(metric_codes),
                         "comparison_policy": summary.get("note", "")},
    }

    if interpreter is not None and visible_facts:
        interpreted = interpreter({"symbol": symbol.upper(), "as_of": cutoff.isoformat(),
                                  "financial_summary": summary, "missing_data": missing,
                                  "risks": risks, "evidence_refs": report["evidence_refs"]})
        if not isinstance(interpreted, dict):
            raise ValueError("fundamental interpreter must return a dictionary")
        # Accept only narrative fields; deterministic evidence and missing-data
        # accounting cannot be replaced by model-generated numbers.
        if isinstance(interpreted.get("summary"), str) and interpreted["summary"].strip():
            report["summary"] = interpreted["summary"].strip()
        if isinstance(interpreted.get("risks"), list):
            report["interpretation_risks"] = interpreted["risks"]
        if isinstance(interpreted.get("confidence"), (int, float)) and not isinstance(interpreted.get("confidence"), bool):
            report["interpretation_confidence"] = max(0.0, min(1.0, float(interpreted["confidence"])))

    trace = list(state.get("workflow_trace") or [])
    trace.append("fundamental_analyst")
    return {"fundamental_analysis": report, "workflow_trace": trace}

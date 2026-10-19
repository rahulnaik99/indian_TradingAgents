"""Final evidence reconciliation for the research-only trading pipeline.

The manager reconciles specialist reports but cannot override a hard risk
rejection. It does not place orders or manufacture evidence.
"""
from __future__ import annotations

from math import isfinite
from typing import Any, Callable

from tradingagents.india.langgraph_pipeline.contracts import validate_final_verdict

_VALID_ACTIONS = {"BUY", "SELL", "NO_TRADE", "NEEDS_REVIEW"}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _confidence(*values: Any) -> float:
    usable = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and isfinite(float(v)) and 0 <= float(v) <= 1]
    return round(sum(usable) / len(usable), 3) if usable else 0.0


def build_manager_verdict(
    state: dict[str, Any],
    *,
    interpreter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a validated verdict from specialist reports and risk review.

    Optional ``interpreter`` can suggest a verdict and narrative. Hard risk
    blocks always win, and unsupported actions or invalid confidence fail closed.
    Evidence references are copied from reports; none are invented here.
    """
    fundamental = state.get("fundamental_analysis") or {}
    technical = state.get("technical_analysis") or {}
    trader = state.get("trader_analysis") or {}
    risk = state.get("risk_analysis") or {}
    reports = (fundamental, technical, trader, risk)
    if any(not isinstance(report, dict) for report in reports):
        raise ValueError("analyst and risk outputs must be dictionaries")

    risk_status = risk.get("status", "not_run")
    hard_blocks = _strings(risk.get("hard_blocks"))
    missing: list[str] = []
    for name, report in (("fundamental", fundamental), ("technical", technical)):
        if report.get("status") not in {"ok", "limited"}:
            missing.append(f"{name} analysis is unavailable or incomplete")
        missing.extend(_strings(report.get("missing_data")))
    missing.extend(_strings(trader.get("missing_data")))
    risk_needs_review = _strings(risk.get("needs_review"))
    missing.extend(risk_needs_review)

    supporting = list(dict.fromkeys(
        _strings(fundamental.get("evidence_refs"))
        + _strings(technical.get("evidence_refs"))
        + _strings(fundamental.get("supporting_evidence"))
        + _strings(technical.get("supporting_evidence"))
    ))
    contradicting = list(dict.fromkeys(
        _strings(fundamental.get("contradicting_evidence"))
        + _strings(technical.get("contradicting_evidence"))
        + _strings(fundamental.get("risks"))
        + _strings(technical.get("risks"))
    ))
    rationale = _strings(trader.get("rationale"))
    rationale.extend(_strings(fundamental.get("summary")))
    rationale.extend(_strings(technical.get("summary")))

    candidate = trader.get("action", "NEEDS_REVIEW")
    if candidate not in _VALID_ACTIONS:
        candidate = "NEEDS_REVIEW"
        rationale.append("Trader action was invalid; manual review is required.")

    # Optional LLM may reconcile narrative, but it cannot bypass missing data or risk blocks.
    interpreted: dict[str, Any] = {}
    if interpreter is not None:
        raw = interpreter({
            "fundamental_analysis": fundamental,
            "technical_analysis": technical,
            "trader_analysis": trader,
            "risk_analysis": risk,
            "missing_data": list(dict.fromkeys(missing)),
            "hard_risk_blocks": hard_blocks,
        })
        if not isinstance(raw, dict):
            raise ValueError("manager interpreter must return a dictionary")
        interpreted = raw

    verdict = interpreted.get("verdict", candidate)
    if verdict not in _VALID_ACTIONS:
        verdict = "NEEDS_REVIEW"
        rationale.append("Manager output contained an unsupported verdict; manual review is required.")

    if hard_blocks or risk_status == "rejected":
        verdict = "NO_TRADE"
        rationale.extend(hard_blocks or ["Risk Analyst rejected the proposed trade."])
    elif risk_status in {"needs_review", "not_run"}:
        verdict = "NEEDS_REVIEW"
        rationale.append("Risk review is incomplete; no directional verdict can be approved.")
    elif missing or fundamental.get("status") not in {"ok", "limited"} or technical.get("status") not in {"ok", "limited"}:
        verdict = "NEEDS_REVIEW"
        rationale.append("Critical evidence is missing or incomplete.")
    elif trader.get("action") in {"NO_TRADE", "NEEDS_REVIEW"}:
        verdict = trader["action"]
    elif risk_status != "approved_for_review":
        verdict = "NEEDS_REVIEW"
        rationale.append("Risk status is not approved for further consideration.")

    # An interpreter's confidence is accepted only if valid; otherwise derive a conservative mean.
    confidence_value = interpreted.get("confidence")
    if isinstance(confidence_value, (int, float)) and not isinstance(confidence_value, bool) and isfinite(float(confidence_value)) and 0 <= float(confidence_value) <= 1:
        confidence = float(confidence_value)
    else:
        confidence = _confidence(fundamental.get("confidence"), technical.get("confidence"))
    if verdict in {"NO_TRADE", "NEEDS_REVIEW"} and (missing or hard_blocks or risk_status != "approved_for_review" or verdict == "NEEDS_REVIEW"):
        confidence = min(confidence, 0.5)

    if fundamental.get("status") not in {"ok", "limited"} or technical.get("status") not in {"ok", "limited"}:
        quality = "insufficient"
    elif missing or any(r.get("status") == "limited" for r in (fundamental, technical)):
        quality = "limited"
    else:
        quality = "good"

    final = {
        "verdict": verdict,
        "rationale": list(dict.fromkeys(rationale + _strings(interpreted.get("rationale")))),
        "supporting_evidence": list(dict.fromkeys(supporting + _strings(interpreted.get("supporting_evidence")))),
        "contradicting_evidence": list(dict.fromkeys(contradicting + _strings(interpreted.get("contradicting_evidence")))),
        "risk_status": risk_status,
        "confidence": confidence,
        "data_quality": quality,
        "missing_data": list(dict.fromkeys(missing)),
        "live_order_placed": False,
    }
    final = validate_final_verdict(final)
    return {
        "manager_analysis": {
            "status": "ok" if quality == "good" else "limited",
            "summary": "; ".join(final["rationale"][:4]),
            "findings": [],
            "risks": final["contradicting_evidence"],
            "evidence_refs": final["supporting_evidence"],
            "confidence": final["confidence"],
            "missing_data": final["missing_data"],
        },
        "final_verdict": final,
        "workflow_trace": ["hedge_fund_manager"],
    }


hedge_fund_manager_node = build_manager_verdict

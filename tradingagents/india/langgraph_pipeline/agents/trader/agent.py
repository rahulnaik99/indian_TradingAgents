"""Evidence-grounded Trader Agent node.

Creates a research-only trade proposal from the Fundamental and Technical
Analyst reports. It does not place orders and does not invent price levels.
"""
from __future__ import annotations

from math import isfinite
from typing import Any, Callable

_ALLOWED = {"BUY", "SELL", "NO_TRADE", "NEEDS_REVIEW"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if isfinite(value) else None


def _report_status(report: Any) -> str:
    return report.get("status", "unavailable") if isinstance(report, dict) else "unavailable"


def _price_levels(technical: dict[str, Any], side: str) -> tuple[float | None, float | None, float | None]:
    """Extract candidate levels only when explicitly supplied by technical output."""
    candidates = technical.get("trade_setup") or technical.get("setup") or {}
    if not isinstance(candidates, dict):
        return None, None, None
    entry = _number(candidates.get("entry"))
    stop = _number(candidates.get("stop_loss", candidates.get("stop")))
    target = _number(candidates.get("target"))
    # Reject levels that are structurally inconsistent with the proposed direction.
    if entry is None or stop is None or target is None:
        return None, None, None
    if side == "BUY" and not (stop < entry < target):
        return None, None, None
    if side == "SELL" and not (target < entry < stop):
        return None, None, None
    return entry, stop, target


def _rr(entry: float, stop: float, target: float) -> float | None:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        return None
    return round(reward / risk, 4)


def build_trade_proposal(
    state: dict[str, Any],
    *,
    interpreter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    minimum_reward_risk: float = 1.5,
) -> dict[str, Any]:
    """LangGraph node that builds a validated, evidence-grounded trade proposal.

    An optional interpreter may recommend a direction, but all price levels are
    sourced from deterministic technical output and validated here. Missing or
    conflicting reports produce NO_TRADE/NEEDS_REVIEW rather than a guess.
    """
    if not isfinite(minimum_reward_risk) or minimum_reward_risk <= 0:
        raise ValueError("minimum_reward_risk must be a positive finite number")

    fundamental = state.get("fundamental_analysis") or {}
    technical_report = state.get("technical_analysis") or {}
    technical_data = state.get("technical_data") or {}
    if not isinstance(fundamental, dict) or not isinstance(technical_report, dict) or not isinstance(technical_data, dict):
        raise ValueError("analyst reports and technical_data must be dictionaries")

    rationale: list[str] = []
    entry_conditions: list[str] = []
    invalidation: list[str] = []
    missing: list[str] = []

    if _report_status(technical_report) not in {"ok", "limited"}:
        missing.append("technical analyst report unavailable")
    if _report_status(fundamental) not in {"ok", "limited"}:
        missing.append("fundamental analyst report unavailable")

    recommendation: dict[str, Any] = {}
    if interpreter is not None and not missing:
        raw = interpreter({
            "fundamental_analysis": fundamental,
            "technical_analysis": technical_report,
            "technical_data": technical_data,
            "minimum_reward_risk": minimum_reward_risk,
        })
        if not isinstance(raw, dict):
            raise ValueError("Trader interpreter must return a dictionary")
        recommendation = raw

    action = recommendation.get("action", "NEEDS_REVIEW" if missing else "NO_TRADE")
    if action not in _ALLOWED:
        action = "NEEDS_REVIEW"
        rationale.append("Trader output contained an unsupported action; manual review required.")

    if missing:
        rationale.extend(missing)
        action = "NEEDS_REVIEW"

    direction = action if action in {"BUY", "SELL"} else None
    entry = stop = target = rr = None

    if direction:
        # Price levels must come from deterministic technical data, never LLM text.
        entry, stop, target = _price_levels(technical_data, direction)
        if entry is None:
            action = "NEEDS_REVIEW"
            direction = None
            rationale.append("Valid deterministic entry, stop-loss, and target levels are unavailable.")
        else:
            rr = _rr(entry, stop, target)  # type: ignore[arg-type]
            if rr is None or rr < minimum_reward_risk:
                action = "NO_TRADE"
                direction = None
                entry = stop = target = rr = None
                rationale.append(f"Candidate setup does not meet minimum reward-to-risk of {minimum_reward_risk:.2f}.")
            else:
                rationale.extend(str(x) for x in recommendation.get("rationale", []) if isinstance(x, str))
                entry_conditions.extend(str(x) for x in recommendation.get("entry_conditions", []) if isinstance(x, str))
                invalidation.extend(str(x) for x in recommendation.get("invalidation_conditions", []) if isinstance(x, str))
                rationale.append("Price levels are sourced from deterministic technical output.")

    if action == "NO_TRADE" and not rationale:
        rationale.append("No sufficiently validated trade setup was provided.")
    if action == "SELL" and not any("short" in str(x).lower() for x in state.get("risk_limits", {}).get("permissions", [])):
        # Do not forbid a SELL research signal; make execution semantics explicit.
        rationale.append("SELL is an analytical direction only; short-selling permissions and instrument rules require separate validation.")

    proposal = {
        "action": action,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        "target": target,
        "reward_risk_ratio": rr,
        "entry_conditions": entry_conditions,
        "invalidation_conditions": invalidation,
        "rationale": rationale,
        "missing_data": missing,
        "status": "ok" if action in {"BUY", "SELL", "NO_TRADE"} else "limited",
        "live_order_placed": False,
    }
    return {"trader_analysis": proposal, "workflow_trace": ["trader_agent"]}


# Conventional node alias for graph builders.
trader_agent_node = build_trade_proposal

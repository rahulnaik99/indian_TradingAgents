"""Deterministic, fail-closed risk review for research trade proposals.

This node never places orders. It validates proposal geometry and configured
risk constraints, and only approves a proposal for further review when the
required inputs are available and no hard block is present.
"""
from __future__ import annotations

from math import isfinite
from typing import Any

_ALLOWED_ACTIONS = {"BUY", "SELL"}
_DEFAULT_LIMITS = {
    "minimum_reward_risk": 1.5,
    "max_stop_distance_pct": 5.0,
    "max_risk_per_trade_pct": 1.0,
    "max_position_exposure_pct": 20.0,
}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _positive_limit(limits: dict[str, Any], key: str, default: float) -> float:
    value = _finite_number(limits.get(key, default))
    if value is None or value <= 0:
        raise ValueError(f"risk limit {key!r} must be a positive finite number")
    return value


def review_trade_risk(state: dict[str, Any]) -> dict[str, Any]:
    """LangGraph node that returns ``risk_analysis`` and trace updates.

    Optional ``portfolio`` fields:
      - ``equity``: current portfolio equity/capital
      - ``current_exposure``: current market exposure in currency units
      - ``proposed_quantity``: proposed number of shares/units

    Optional ``risk_limits`` can override the defaults. Unknown or unavailable
    portfolio information is not fabricated; checks that require it are marked
    as not evaluated. A directional proposal is not approved if critical price
    or risk inputs are missing.
    """
    proposal = state.get("trader_analysis") or {}
    if not isinstance(proposal, dict):
        raise ValueError("trader_analysis must be a dictionary")
    configured = state.get("risk_limits") or {}
    if not isinstance(configured, dict):
        raise ValueError("risk_limits must be a dictionary")
    portfolio = state.get("portfolio") or {}
    if not isinstance(portfolio, dict):
        raise ValueError("portfolio must be a dictionary")

    limits = {
        "minimum_reward_risk": _positive_limit(configured, "minimum_reward_risk", _DEFAULT_LIMITS["minimum_reward_risk"]),
        "max_stop_distance_pct": _positive_limit(configured, "max_stop_distance_pct", _DEFAULT_LIMITS["max_stop_distance_pct"]),
        "max_risk_per_trade_pct": _positive_limit(configured, "max_risk_per_trade_pct", _DEFAULT_LIMITS["max_risk_per_trade_pct"]),
        "max_position_exposure_pct": _positive_limit(configured, "max_position_exposure_pct", _DEFAULT_LIMITS["max_position_exposure_pct"]),
    }

    checks: list[dict[str, Any]] = []
    hard_blocks: list[str] = []
    warnings: list[str] = []
    needs_review: list[str] = []
    action = proposal.get("action", "NEEDS_REVIEW")

    def record(name: str, status: str, detail: str, value: Any = None) -> None:
        item: dict[str, Any] = {"name": name, "status": status, "detail": detail}
        if value is not None:
            item["value"] = value
        checks.append(item)

    if action not in _ALLOWED_ACTIONS:
        # No trade is a valid, non-risky terminal outcome. NEEDS_REVIEW remains
        # unresolved rather than being misreported as approved.
        if action == "NO_TRADE":
            record("proposal_action", "pass", "Trader Agent did not propose a directional trade.")
            status = "approved_for_review"
        else:
            record("proposal_action", "needs_review", "No valid directional trade proposal is available.")
            needs_review.append("A valid BUY/SELL proposal is required for risk approval.")
            status = "needs_review"
        return {"risk_analysis": {
            "status": status, "checks": checks, "hard_blocks": hard_blocks,
            "warnings": warnings, "needs_review": needs_review,
            "limits_applied": limits, "live_order_placed": False,
        }, "workflow_trace": ["risk_analyst"]}

    entry = _finite_number(proposal.get("entry"))
    stop = _finite_number(proposal.get("stop_loss"))
    target = _finite_number(proposal.get("target"))
    rr = _finite_number(proposal.get("reward_risk_ratio"))
    if entry is None or stop is None or target is None or entry <= 0:
        needs_review.append("Entry, stop-loss, and target must be valid positive numeric prices.")
        record("price_levels", "needs_review", needs_review[-1])
    else:
        geometry_ok = (action == "BUY" and stop < entry < target) or (action == "SELL" and target < entry < stop)
        if not geometry_ok:
            hard_blocks.append("Price levels are inconsistent with the proposed direction.")
            record("price_geometry", "fail", hard_blocks[-1])
        else:
            record("price_geometry", "pass", "Entry, stop-loss, and target ordering is consistent.")
            stop_pct = abs(entry - stop) / entry * 100.0
            if stop_pct > limits["max_stop_distance_pct"]:
                hard_blocks.append(f"Stop distance {stop_pct:.2f}% exceeds limit {limits['max_stop_distance_pct']:.2f}%.")
                record("stop_distance", "fail", hard_blocks[-1], round(stop_pct, 4))
            else:
                record("stop_distance", "pass", "Stop distance is within the configured limit.", round(stop_pct, 4))

            calculated_rr = abs(target - entry) / abs(entry - stop)
            if rr is not None and abs(rr - calculated_rr) > max(0.01, calculated_rr * 0.02):
                needs_review.append("Reported reward-to-risk does not match the supplied price levels.")
                record("reward_risk_consistency", "needs_review", needs_review[-1], round(calculated_rr, 4))
            elif calculated_rr < limits["minimum_reward_risk"]:
                hard_blocks.append(f"Reward-to-risk {calculated_rr:.2f} is below minimum {limits['minimum_reward_risk']:.2f}.")
                record("reward_risk", "fail", hard_blocks[-1], round(calculated_rr, 4))
            else:
                record("reward_risk", "pass", "Reward-to-risk meets the configured minimum.", round(calculated_rr, 4))

    equity = _finite_number(portfolio.get("equity"))
    quantity = _finite_number(portfolio.get("proposed_quantity"))
    current_exposure = _finite_number(portfolio.get("current_exposure"))
    if equity is None or equity <= 0:
        warnings.append("Portfolio equity unavailable; portfolio-level risk percentage cannot be evaluated.")
        record("portfolio_equity", "not_evaluated", warnings[-1])
    elif entry is not None and stop is not None and quantity is not None and quantity > 0:
        risk_amount = abs(entry - stop) * quantity
        risk_pct = risk_amount / equity * 100.0
        if risk_pct > limits["max_risk_per_trade_pct"]:
            hard_blocks.append(f"Proposed loss at stop {risk_pct:.2f}% of equity exceeds limit {limits['max_risk_per_trade_pct']:.2f}%.")
            record("risk_per_trade", "fail", hard_blocks[-1], round(risk_pct, 4))
        else:
            record("risk_per_trade", "pass", "Proposed loss at stop is within the configured equity risk limit.", round(risk_pct, 4))
    else:
        warnings.append("Proposed quantity unavailable; position-level loss cannot be calculated.")
        record("risk_per_trade", "not_evaluated", warnings[-1])

    if equity is not None and equity > 0 and entry is not None and quantity is not None and quantity > 0:
        exposure = (current_exposure or 0.0) + entry * quantity
        exposure_pct = exposure / equity * 100.0
        if exposure_pct > limits["max_position_exposure_pct"]:
            hard_blocks.append(f"Estimated total exposure {exposure_pct:.2f}% exceeds limit {limits['max_position_exposure_pct']:.2f}%.")
            record("portfolio_exposure", "fail", hard_blocks[-1], round(exposure_pct, 4))
        else:
            record("portfolio_exposure", "pass", "Estimated total exposure is within the configured limit.", round(exposure_pct, 4))
    else:
        record("portfolio_exposure", "not_evaluated", "Equity, entry, and proposed quantity are required for exposure calculation.")

    if hard_blocks:
        status = "rejected"
    elif needs_review:
        status = "needs_review"
    else:
        status = "approved_for_review"
        if warnings:
            # Approval is only for further review; missing portfolio sizing is
            # disclosed and does not mean the trade is cleared for execution.
            pass

    return {"risk_analysis": {
        "status": status,
        "checks": checks,
        "hard_blocks": hard_blocks,
        "warnings": warnings,
        "needs_review": needs_review,
        "limits_applied": limits,
        "live_order_placed": False,
    }, "workflow_trace": ["risk_analyst"]}


risk_analyst_node = review_trade_risk

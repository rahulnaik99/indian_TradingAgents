"""Deterministic market-regime and breadth interpretation for India evidence."""
from __future__ import annotations

from typing import Any


def analyze_market_regime(market_context: dict[str, Any] | None) -> dict[str, Any]:
    """Classify breadth conservatively; never infer a regime from unavailable data.

    Breadth comes from the configured scanner universe (currently NIFTY 50), not
    from a census of all NSE-listed securities. Thresholds are descriptive rules,
    not a predictive model or a trading signal.
    """
    context = market_context if isinstance(market_context, dict) else {}
    if context.get("status") != "available":
        return {
            "status": "unavailable", "regime": "unknown", "breadth_state": "unknown",
            "coverage_count": 0, "advancers": None, "decliners": None,
            "advance_decline_ratio": None, "selected_stock_bias": None,
            "alignment": "unknown",
            "reason": context.get("reason") or "No eligible market-movers snapshot was available.",
            "scope": "No market-wide conclusion",
        }

    breadth = context.get("market_breadth") if isinstance(context.get("market_breadth"), dict) else {}
    adv = breadth.get("advancers")
    dec = breadth.get("decliners")
    coverage = breadth.get("coverage_count", 0)
    adv = adv if isinstance(adv, int) and not isinstance(adv, bool) and adv >= 0 else None
    dec = dec if isinstance(dec, int) and not isinstance(dec, bool) and dec >= 0 else None
    coverage = coverage if isinstance(coverage, int) and not isinstance(coverage, bool) and coverage >= 0 else 0
    ratio = round(adv / dec, 3) if adv is not None and dec is not None and dec > 0 else (None if adv is None or dec is None else (999.0 if adv > 0 and dec == 0 else 1.0))

    if coverage < 10 or adv is None or dec is None:
        breadth_state = "insufficient_coverage"
    elif adv >= dec * 1.5 and adv - dec >= 3:
        breadth_state = "positive_breadth"
    elif dec >= adv * 1.5 and dec - adv >= 3:
        breadth_state = "negative_breadth"
    else:
        breadth_state = "mixed_breadth"

    selected = context.get("selected_stock") if isinstance(context.get("selected_stock"), dict) else {}
    stock_bias = selected.get("multi_timeframe_bias")
    if breadth_state == "positive_breadth":
        regime = "risk_on_breadth"
    elif breadth_state == "negative_breadth":
        regime = "risk_off_breadth"
    elif breadth_state == "mixed_breadth":
        regime = "mixed_breadth"
    else:
        regime = "unknown"

    if regime == "risk_on_breadth" and stock_bias == "bullish_alignment":
        alignment = "supportive"
    elif regime == "risk_off_breadth" and stock_bias == "bearish_alignment":
        alignment = "supportive_for_bearish_setup"
    elif regime in {"risk_on_breadth", "risk_off_breadth"} and stock_bias in {"bullish_alignment", "bearish_alignment"}:
        alignment = "divergent"
    elif regime == "mixed_breadth":
        alignment = "mixed"
    else:
        alignment = "unknown"

    warnings = list(context.get("warnings", [])) if isinstance(context.get("warnings"), list) else []
    warnings.append("Breadth reflects the configured scanner subset, not all NSE stocks; descriptive classification only.")
    return {
        "status": "available", "regime": regime, "breadth_state": breadth_state,
        "coverage_count": coverage, "advancers": adv, "decliners": dec,
        "unchanged": breadth.get("unchanged"), "advance_decline_ratio": ratio,
        "selected_stock_bias": stock_bias, "alignment": alignment,
        "snapshot_as_of": context.get("as_of"), "snapshot_age_hours": context.get("snapshot_age_hours"),
        "universe": context.get("universe"), "source": context.get("source"),
        "warnings": warnings,
        "interpretation": "Market regime is contextual evidence only; it does not independently trigger or prohibit a trade.",
    }

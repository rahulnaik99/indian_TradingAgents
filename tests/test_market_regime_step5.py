from tradingagents.india.market_regime import analyze_market_regime
from tradingagents.india.workflow import _normalize_evidence, _assess_risk


def test_unavailable_market_context_does_not_infer_regime():
    result = analyze_market_regime({"status": "unavailable", "reason": "stale"})
    assert result["regime"] == "unknown"
    assert result["alignment"] == "unknown"
    assert result["advance_decline_ratio"] is None


def test_positive_breadth_supports_bullish_stock_but_is_not_signal():
    result = analyze_market_regime({
        "status": "available", "source": "saved scanner", "as_of": "2026-10-15T09:00:00+05:30",
        "market_breadth": {"advancers": 18, "decliners": 5, "unchanged": 2, "coverage_count": 25},
        "selected_stock": {"multi_timeframe_bias": "bullish_alignment"},
        "warnings": [], "universe": {"scope": "NIFTY 50 subset"},
    })
    assert result["regime"] == "risk_on_breadth"
    assert result["alignment"] == "supportive"
    assert "not a standalone trade signal" in result["interpretation"]


def test_divergent_stock_and_market_adds_workflow_risk_flag():
    state = {
        "symbol": "ABC", "as_of": "2026-10-15T09:00:00+05:30",
        "technical": {"multi_timeframe_bias": "bullish_alignment"},
        "events_input": [], "facts_input": [], "calendar_events_input": [],
        "market_context_input": {
            "status": "available", "market_breadth": {"advancers": 4, "decliners": 20, "unchanged": 1, "coverage_count": 25},
            "selected_stock": {"multi_timeframe_bias": "bullish_alignment"}, "warnings": [],
        },
    }
    normalized = _normalize_evidence(state)
    state.update(normalized)
    result = _assess_risk(state)
    assert result["assessment"]["market_regime"]["regime"] == "risk_off_breadth"
    assert any(flag.get("type") == "stock_market_divergence" for flag in result["risk_flags"])

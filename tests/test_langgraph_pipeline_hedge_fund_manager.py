from tradingagents.india.langgraph_pipeline.agents.hedge_fund_manager.agent import build_manager_verdict


def base_state():
    return {
        "fundamental_analysis": {"status": "ok", "summary": "Revenue growth is positive.", "confidence": 0.8, "evidence_refs": ["annual-report"]},
        "technical_analysis": {"status": "ok", "summary": "Higher-timeframe trend is bullish.", "confidence": 0.7, "evidence_refs": ["ohlcv-snapshot"]},
        "trader_analysis": {"action": "BUY", "rationale": ["Breakout setup"], "missing_data": []},
        "risk_analysis": {"status": "approved_for_review", "hard_blocks": [], "needs_review": []},
    }


def test_valid_candidate_passes_to_review():
    result = build_manager_verdict(base_state())
    assert result["final_verdict"]["verdict"] == "BUY"
    assert result["final_verdict"]["data_quality"] == "good"
    assert result["final_verdict"]["live_order_placed"] is False
    assert "annual-report" in result["final_verdict"]["supporting_evidence"]


def test_hard_risk_rejection_cannot_be_overridden():
    state = base_state()
    state["risk_analysis"] = {"status": "rejected", "hard_blocks": ["Exposure limit breached"], "needs_review": []}
    result = build_manager_verdict(state, interpreter=lambda _: {"verdict": "BUY", "confidence": 0.99})
    assert result["final_verdict"]["verdict"] == "NO_TRADE"
    assert "Exposure limit breached" in result["final_verdict"]["rationale"]


def test_missing_analyst_report_routes_to_review():
    state = base_state()
    state["fundamental_analysis"] = {"status": "unavailable", "missing_data": ["financial statements"]}
    result = build_manager_verdict(state)
    assert result["final_verdict"]["verdict"] == "NEEDS_REVIEW"
    assert result["final_verdict"]["data_quality"] == "insufficient"


def test_risk_needs_review_blocks_directional_verdict():
    state = base_state()
    state["risk_analysis"] = {"status": "needs_review", "hard_blocks": [], "needs_review": ["quantity missing"]}
    result = build_manager_verdict(state)
    assert result["final_verdict"]["verdict"] == "NEEDS_REVIEW"
    assert result["final_verdict"]["confidence"] <= 0.5


def test_invalid_interpreter_verdict_fails_closed():
    result = build_manager_verdict(base_state(), interpreter=lambda _: {"verdict": "MOON", "confidence": 1.4})
    assert result["final_verdict"]["verdict"] == "NEEDS_REVIEW"
    assert result["final_verdict"]["confidence"] <= 0.5


def test_no_trade_is_preserved():
    state = base_state()
    state["trader_analysis"] = {"action": "NO_TRADE", "rationale": ["No setup"], "missing_data": []}
    result = build_manager_verdict(state)
    assert result["final_verdict"]["verdict"] == "NO_TRADE"

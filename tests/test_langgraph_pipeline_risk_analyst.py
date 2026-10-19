import pytest

from tradingagents.india.langgraph_pipeline.agents.risk_analyst.agent import review_trade_risk


def state(**overrides):
    base = {"trader_analysis": {
        "action": "BUY", "entry": 100.0, "stop_loss": 95.0,
        "target": 110.0, "reward_risk_ratio": 2.0,
    }}
    base.update(overrides)
    return base


def test_valid_proposal_approved_for_review_not_execution():
    result = review_trade_risk(state())["risk_analysis"]
    assert result["status"] == "approved_for_review"
    assert result["hard_blocks"] == []
    assert result["live_order_placed"] is False


def test_no_trade_is_non_directional_terminal_outcome():
    result = review_trade_risk(state(trader_analysis={"action": "NO_TRADE"}))["risk_analysis"]
    assert result["status"] == "approved_for_review"


def test_inconsistent_price_geometry_rejected():
    proposal = {"action": "BUY", "entry": 100, "stop_loss": 105, "target": 110, "reward_risk_ratio": 2}
    result = review_trade_risk(state(trader_analysis=proposal))["risk_analysis"]
    assert result["status"] == "rejected"
    assert any("inconsistent" in x for x in result["hard_blocks"])


def test_stop_distance_over_limit_rejected():
    proposal = {"action": "BUY", "entry": 100, "stop_loss": 90, "target": 120, "reward_risk_ratio": 2}
    result = review_trade_risk(state(trader_analysis=proposal))["risk_analysis"]
    assert result["status"] == "rejected"
    assert any("Stop distance" in x for x in result["hard_blocks"])


def test_low_reward_risk_rejected():
    proposal = {"action": "BUY", "entry": 100, "stop_loss": 95, "target": 106, "reward_risk_ratio": 1.2}
    result = review_trade_risk(state(trader_analysis=proposal))["risk_analysis"]
    assert result["status"] == "rejected"
    assert any("Reward-to-risk" in x for x in result["hard_blocks"])


def test_mismatched_reported_rr_requires_review():
    proposal = {"action": "BUY", "entry": 100, "stop_loss": 95, "target": 110, "reward_risk_ratio": 1.0}
    result = review_trade_risk(state(trader_analysis=proposal))["risk_analysis"]
    assert result["status"] == "needs_review"


def test_position_risk_limit_rejected():
    portfolio = {"equity": 10000, "proposed_quantity": 30, "current_exposure": 0}
    result = review_trade_risk(state(portfolio=portfolio))["risk_analysis"]
    assert result["status"] == "rejected"
    assert any("exceeds limit" in x for x in result["hard_blocks"])


def test_missing_portfolio_inputs_are_disclosed():
    result = review_trade_risk(state())["risk_analysis"]
    assert result["warnings"]
    assert any(check["status"] == "not_evaluated" for check in result["checks"])


def test_invalid_risk_limit_raises():
    with pytest.raises(ValueError):
        review_trade_risk(state(risk_limits={"max_stop_distance_pct": 0}))

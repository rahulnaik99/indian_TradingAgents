from tradingagents.india.langgraph_pipeline.agents.trader.agent import build_trade_proposal


def base_state():
    return {
        "fundamental_analysis": {"status": "ok", "summary": "stable", "findings": []},
        "technical_analysis": {"status": "ok", "summary": "setup present", "findings": []},
        "technical_data": {"trade_setup": {"entry": 100.0, "stop_loss": 95.0, "target": 110.0}},
    }


def test_missing_reports_require_review():
    result = build_trade_proposal({})["trader_analysis"]
    assert result["action"] == "NEEDS_REVIEW"
    assert result["live_order_placed"] is False


def test_no_trade_by_default_without_interpreter():
    result = build_trade_proposal(base_state())["trader_analysis"]
    assert result["action"] == "NO_TRADE"
    assert result["entry"] is None


def test_buy_with_valid_levels_and_rr():
    state = base_state()
    result = build_trade_proposal(state, interpreter=lambda _: {
        "action": "BUY", "rationale": ["Higher-timeframe trend aligned"],
        "entry_conditions": ["Breakout confirmed"],
        "invalidation_conditions": ["Close below support"],
    })["trader_analysis"]
    assert result["action"] == "BUY"
    assert result["entry"] == 100.0
    assert result["stop_loss"] == 95.0
    assert result["target"] == 110.0
    assert result["reward_risk_ratio"] == 2.0
    assert result["live_order_placed"] is False


def test_bad_levels_force_review():
    state = base_state()
    state["technical_data"]["trade_setup"] = {"entry": 100, "stop_loss": 105, "target": 110}
    result = build_trade_proposal(state, interpreter=lambda _: {"action": "BUY"})["trader_analysis"]
    assert result["action"] == "NEEDS_REVIEW"
    assert result["entry"] is None


def test_low_reward_risk_becomes_no_trade():
    state = base_state()
    state["technical_data"]["trade_setup"] = {"entry": 100, "stop_loss": 95, "target": 106}
    result = build_trade_proposal(state, interpreter=lambda _: {"action": "BUY"})["trader_analysis"]
    assert result["action"] == "NO_TRADE"


def test_invalid_interpreter_action_is_not_accepted():
    result = build_trade_proposal(base_state(), interpreter=lambda _: {"action": "YOLO"})["trader_analysis"]
    assert result["action"] == "NEEDS_REVIEW"

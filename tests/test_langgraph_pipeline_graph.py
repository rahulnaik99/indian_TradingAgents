from __future__ import annotations

import pytest

from tradingagents.india.langgraph_pipeline.graph import (
    _route_after_risk,
    _risk_rejection_node,
    _validate_final_node,
    create_trading_analysis_graph,
)


def test_risk_rejection_routes_away_from_manager():
    assert _route_after_risk({"risk_analysis": {"status": "rejected", "hard_blocks": []}}) == "risk_rejection"
    assert _route_after_risk({"risk_analysis": {"status": "approved_for_review", "hard_blocks": []}}) == "hedge_fund_manager"


def test_hard_block_routes_to_risk_rejection():
    assert _route_after_risk({"risk_analysis": {"status": "needs_review", "hard_blocks": ["max loss exceeded"]}}) == "risk_rejection"


def test_risk_rejection_node_emits_no_trade_and_never_claims_execution():
    result = _risk_rejection_node({"risk_analysis": {"status": "rejected", "hard_blocks": ["risk cap exceeded"]}})
    verdict = result["final_verdict"]
    assert verdict["verdict"] == "NO_TRADE"
    assert verdict["risk_status"] == "rejected"
    assert verdict["live_order_placed"] is False


def test_invalid_final_verdict_fails_closed():
    result = _validate_final_node({"final_verdict": {"verdict": "BUY", "confidence": 8}})
    assert result["final_verdict"]["verdict"] == "NEEDS_REVIEW"
    assert result["status"] == "partial"
    assert result["errors"]


def test_graph_builder_gives_actionable_message_without_langgraph():
    try:
        import langgraph  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="LangGraph is required"):
            create_trading_analysis_graph()
    else:
        pytest.skip("LangGraph is installed; compile smoke test belongs in the integration suite")

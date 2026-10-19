from tradingagents.india.portfolio_risk import assess_portfolio_risk
from tradingagents.india.workflow import run_integrated_research


def test_missing_portfolio_is_not_assumed_flat():
    result = assess_portfolio_risk(symbol="SBIN", portfolio=None, current_price=800)
    assert result["status"] == "not_provided"
    assert "cannot be assessed" in result["warnings"][0]


def test_position_and_configured_limit_breach_are_calculated():
    result = assess_portfolio_risk(
        symbol="SBIN",
        portfolio={"cash": 50000, "currency": "INR", "positions": [{"ticker": "SBIN", "quantity": 100}]},
        current_price=800,
        limits={"max_position_value_pct": 50},
    )
    assert result["status"] == "limit_breach"
    assert any(c.get("name") == "max_position_value_pct" and c["status"] == "breach" for c in result["checks"])


def test_unknown_other_holding_price_prevents_invented_gross_exposure():
    result = assess_portfolio_risk(
        symbol="SBIN",
        portfolio={"cash": 50000, "positions": [
            {"ticker": "SBIN", "quantity": 10}, {"ticker": "INFY", "quantity": 5}
        ]},
        current_price=800,
        limits={"max_gross_exposure_pct": 80},
    )
    assert not any(c.get("name") == "gross_exposure" for c in result["checks"])
    assert any("lack current prices" in w for w in result["warnings"])


def test_integrated_workflow_exposes_portfolio_risk_and_flags_breach():
    result = run_integrated_research(
        symbol="SBIN", as_of="2026-10-10T10:00:00+05:30",
        technical={"multi_timeframe_bias": "bullish_alignment", "current_price": 800},
        events=[], facts=[], portfolio={"cash": 50000, "positions": [{"ticker": "SBIN", "quantity": 100}]},
        portfolio_risk_limits={"max_position_value_pct": 50}, use_llm=False,
    )
    assert result["portfolio_risk"]["status"] == "limit_breach"
    assert any(flag.get("type") == "portfolio_limit_breach" for flag in result["assessment"]["risk_flags"])
    assert result["assessment"]["live_order_placed"] is False

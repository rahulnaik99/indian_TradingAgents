from tradingagents.india.workflow import run_integrated_research


def test_fundamental_health_flags_flow_into_risk_assessment():
    health = {
        "red_flags": [{"severity": "red", "code": "latest_quarter_loss",
                       "title": "Latest quarter reported a net loss",
                       "detail": "-12 cr", "periods": ["2026-06-30"]}],
        "green_flags": [{"severity": "green", "code": "cash_flow_exceeds_profit",
                         "title": "Operating cash flow exceeds net income",
                         "detail": "OCF 20 vs NI 10", "periods": ["2026-06-30"]}],
    }
    result = run_integrated_research(
        symbol="TEST", as_of="2026-10-15T10:00:00+05:30",
        technical={"multi_timeframe_bias": "mixed_or_neutral", "timeframes": {}},
        events=[], facts=[], financial_health_flags=health,
        fundamental_analysis={"status": "available", "statement_periods": {"annual": [], "quarterly": ["2026-06-30"]}},
        use_llm=False,
    )
    assert result["assessment"]["fundamental_health_status"] == "available"
    assert result["assessment"]["fundamental_health_red_flags"] == 1
    assert any(flag.get("type") == "fundamental_health_flag" and flag.get("code") == "latest_quarter_loss"
               for flag in result["assessment"]["risk_flags"])
    assert result["assessment"]["live_order_placed"] is False

"""Regression tests for integrated Analyze evidence and risk propagation."""
from tradingagents.india.workflow import run_integrated_research

AS_OF = "2026-10-15T10:00:00+05:30"


def run(**overrides):
    args = dict(
        symbol="SBIN",
        as_of=AS_OF,
        technical={"multi_timeframe_bias": "bullish_alignment", "current_price": 800.0},
        events=[
            {"title": "eligible news", "published_at": "2026-10-15T03:00:00Z", "severity": "low"},
            {"title": "future news", "published_at": "2026-10-15T06:00:00Z", "severity": "high"},
            {"title": "unknown timestamp", "severity": "high"},
        ],
        facts=[
            {"metric_code": "revenue", "published_at": "2026-10-15T03:00:00Z", "period_end": "2026-09-30"},
            {"metric_code": "profit", "published_at": "2026-10-15T07:00:00Z", "period_end": "2026-09-30"},
        ],
        calendar_events=[
            {"title": "SBIN results", "symbol": "SBIN", "category": "quarterly_results", "event_date": "2026-10-18", "known_at": "2026-10-14T10:00:00+05:30", "verified": True, "importance": "high"},
            {"title": "future-known event", "symbol": "SBIN", "category": "dividend", "event_date": "2026-10-18", "known_at": "2026-10-15T11:00:00+05:30", "verified": True, "importance": "high"},
            {"title": "unknown-known-at event", "symbol": "SBIN", "category": "earnings", "event_date": "2026-10-18"},
            {"title": "other symbol event", "symbol": "TCS", "category": "earnings", "event_date": "2026-10-18", "known_at": "2026-10-14T10:00:00+05:30"},
        ],
        market_context={"status": "available", "market_breadth": {"advancers": 5, "decliners": 20, "coverage_count": 25}, "selected_stock": {"multi_timeframe_bias": "bullish_alignment"}, "as_of": AS_OF, "source": "test"},
        portfolio={"cash": 50000, "net_liquidation_value": 130000, "positions": [{"ticker": "SBIN", "quantity": 100}]},
        portfolio_risk_limits={"max_position_value_pct": 50},
        use_llm=False,
    )
    args.update(overrides)
    return run_integrated_research(**args)


def test_point_in_time_filters_news_and_financial_facts():
    result = run()
    assert [e["title"] for e in result["evidence"]["events"]] == ["eligible news"]
    assert len(result["evidence"]["facts"]) == 1
    assert result["evidence"]["excluded_events_unknown_or_future_timestamp"] == 2
    assert result["evidence"]["excluded_facts_unknown_or_future_timestamp"] == 1


def test_catalysts_exclude_future_unknown_and_other_symbol_events():
    result = run()
    catalysts = result["assessment"]["corporate_catalysts"]
    assert [e["title"] for e in catalysts["upcoming_catalysts"]] == ["SBIN results"]
    assert catalysts["excluded_future_known_at"] == 1
    assert catalysts["excluded_unknown_known_at"] == 1
    assert catalysts["excluded_other_symbol"] == 1
    assert result["assessment"]["calendar_event_count_used"] == 1
    assert result["assessment"]["calendar_event_count_received"] == 4


def test_market_regime_and_portfolio_breach_reach_final_assessment():
    result = run()
    assessment = result["assessment"]
    assert assessment["market_regime"]["regime"] == "risk_off_breadth"
    assert assessment["market_regime"]["alignment"] == "divergent"
    assert assessment["portfolio_risk"]["status"] == "limit_breach"
    assert any(flag.get("type") == "portfolio_limit_breach" for flag in assessment["risk_flags"])
    assert result["portfolio_risk"]["status"] == "limit_breach"
    assert result["llm_used"] is False
    assert result["workflow_engine"] in {"langgraph", "sequential_fallback"}


def test_missing_optional_evidence_does_not_crash():
    result = run(events=[], facts=[], calendar_events=[], market_context={"status": "unavailable"}, portfolio=None)
    assert result["assessment"]["market_regime"]["regime"] == "unknown"
    assert result["assessment"]["portfolio_risk"]["status"] == "not_provided"
    assert result["assessment"]["risk_flag_count"] >= 0

from datetime import datetime, timezone

from tradingagents.india.workflow import run_integrated_research


def test_integrated_workflow_filters_future_evidence_and_returns_risk_flags():
    result = run_integrated_research(
        symbol="SBIN",
        as_of="2026-10-10T10:00:00+05:30",
        technical={"multi_timeframe_bias": "mixed_bearish_bias", "timeframes": {}},
        events=[
            {"event_id": "e1", "title": "RBI audit announced", "event_type": "regulatory_action", "severity": "high",
             "direction": "negative", "source_url": "https://rbi.org.in/notice", "published_at": "2026-10-10T08:00:00+05:30"},
            {"event_id": "e2", "title": "Future event", "event_type": "company_news", "severity": "high",
             "direction": "negative", "source_url": "https://example.com/future", "published_at": "2026-10-11T08:00:00+05:30"},
            {"event_id": "e3", "title": "Undated event", "event_type": "company_news", "severity": "high",
             "direction": "negative", "source_url": "https://example.com/undated", "published_at": None},
        ],
        facts=[
            {"symbol": "SBIN", "metric_code": "net_profit", "value": 90, "unit": "INR_crore", "scope": "consolidated",
             "period_end": "2026-06-30", "published_at": "2026-08-01T10:00:00+05:30", "source_url": "https://nseindia.com/result",
             "qoq": {"percentage_change": -10.0}},
            {"symbol": "SBIN", "metric_code": "net_profit", "value": 1, "unit": "INR_crore", "scope": "consolidated",
             "period_end": "2026-09-30", "published_at": "2026-11-01T10:00:00+05:30", "source_url": "https://nseindia.com/future",
             "qoq": {"percentage_change": -90.0}},
        ],
        use_llm=False,
    )
    assert result["llm_used"] is False
    assert len(result["evidence"]["events"]) == 1
    assert len(result["evidence"]["facts"]) == 1
    assert result["evidence"]["excluded_events_unknown_or_future_timestamp"] == 2
    assert result["assessment"]["risk_flag_count"] >= 2
    assert result["assessment"]["live_order_placed"] is False

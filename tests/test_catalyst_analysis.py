from tradingagents.india.catalyst_analysis import analyze_catalysts


def test_future_known_at_calendar_event_is_excluded():
    result = analyze_catalysts(calendar_events=[{"date": "2026-10-20", "title": "Q2 results", "category": "quarterly_result", "created_at": "2026-10-16T10:00:00+00:00", "importance": "high"}], news_events=[], as_of="2026-10-15T10:00:00+00:00", symbol="RELIANCE")
    assert result["upcoming_catalysts"] == []
    assert result["excluded_future_known_at"] == 1


def test_known_upcoming_corporate_event_is_direction_neutral():
    result = analyze_catalysts(calendar_events=[{"date": "2026-10-20", "title": "Quarterly results", "category": "quarterly_result", "symbol": "RELIANCE", "created_at": "2026-10-14T10:00:00+00:00", "importance": "high", "verified": True, "source_url": "https://example.com"}], news_events=[], as_of="2026-10-15T10:00:00+00:00", symbol="RELIANCE")
    assert len(result["upcoming_catalysts"]) == 1
    assert result["upcoming_catalysts"][0]["direction"] == "unknown"
    assert result["material_upcoming_count"] == 1


def test_unknown_known_at_is_excluded_for_historical_integrity():
    result = analyze_catalysts(calendar_events=[{"date": "2026-10-20", "title": "Dividend"}], news_events=[], as_of="2026-10-15T10:00:00+00:00")
    assert result["excluded_unknown_known_at"] == 1
    assert result["upcoming_catalysts"] == []

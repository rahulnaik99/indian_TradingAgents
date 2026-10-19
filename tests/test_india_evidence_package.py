from tradingagents.india.evidence_package import build_evidence_package, serialize_agent_context


def test_evidence_package_excludes_future_and_undated_events_and_facts():
    package = build_evidence_package(
        symbol="SBIN",
        as_of="2026-10-10T10:00:00+05:30",
        technical={"multi_timeframe_bias": "mixed_or_neutral", "timeframes": {
            "1d": {"status": "ok", "count": 250, "latest_close": 100.0},
            "4h": {"status": "ok", "count": 60},
            "1h": {"status": "ok", "count": 240},
        }},
        events=[
            {"title": "Published event", "published_at": "2026-10-10T09:00:00+05:30", "source_url": "https://nseindia.com/a"},
            {"title": "Future event", "published_at": "2026-10-10T11:00:00+05:30", "source_url": "https://nseindia.com/b"},
            {"title": "Undated event", "published_at": None, "source_url": "https://nseindia.com/c"},
        ],
        facts=[
            {"metric_code": "net_profit", "value": 10, "unit": "INR_crore", "period_end": "2026-06-30", "scope": "consolidated", "published_at": "2026-08-01T10:00:00+05:30", "source_url": "https://nseindia.com/fact"},
            {"metric_code": "revenue", "value": 20, "unit": "INR_crore", "period_end": "2026-09-30", "published_at": "2026-11-01T10:00:00+05:30"},
            {"metric_code": "assets", "value": 30, "unit": "INR_crore", "period_end": "2026-03-31"},
        ],
    )
    assert [row["title"] for row in package["news_and_events"]] == ["Published event"]
    assert [row["metric_code"] for row in package["financial_facts"]] == ["net_profit"]
    assert package["financial_facts"][0]["unit"] == "INR_crore"
    assert package["financial_facts"][0]["period_end"] == "2026-06-30"
    assert package["data_quality"]["excluded_event_unknown_timestamp_count"] == 1
    assert package["data_quality"]["excluded_event_future_timestamp_count"] == 1
    assert package["data_quality"]["excluded_fact_unknown_timestamp_count"] == 1
    assert package["data_quality"]["excluded_fact_future_timestamp_count"] == 1


def test_evidence_package_withholds_wigolo_brief_if_citation_timestamps_are_not_safe():
    package = build_evidence_package(
        symbol="SBIN", as_of="2026-10-10T10:00:00+05:30",
        technical={"timeframes": {"1d": {"status": "ok"}, "4h": {"status": "ok"}, "1h": {"status": "ok"}}},
        events=[], facts=[],
        research={"source": "wigolo", "result": {"brief": "Potentially mixed-date brief", "sources": [
            {"url": "https://nseindia.com/a", "title": "Known date", "published_at": "2026-10-09T10:00:00+05:30"},
            {"url": "https://example.com/b", "title": "Unknown date"},
        ]}},
    )
    assert package["web_research"]["brief"] is None
    assert len(package["web_research"]["sources"]) == 1
    assert any("synthesized brief was withheld" in warning for warning in package["data_quality"]["warnings"])


def test_agent_context_is_json_and_bounded():
    package = build_evidence_package(
        symbol="SBIN", as_of="2026-10-10T10:00:00+05:30",
        technical={"timeframes": {"1d": {"status": "ok"}, "4h": {"status": "ok"}, "1h": {"status": "ok"}}},
        events=[], facts=[],
    )
    import json
    payload = serialize_agent_context(package, max_chars=24000)
    assert len(payload) <= 24000
    decoded = json.loads(payload)
    assert decoded["schema_version"] == "1.0"
    assert decoded["agent_instructions"]

from tradingagents.india.langgraph_pipeline.agents.fundamental_analyst.agent import analyze_fundamentals


def _fact(metric, value, period_end, published_at, period_type="quarterly"):
    return {"symbol": "INFY", "metric_code": metric, "value": value, "unit": "INR",
            "currency": "INR", "period_start": period_end, "period_end": period_end,
            "period_type": period_type, "scope": "consolidated", "source_name": "test source",
            "source_url": "https://example.test/filing", "published_at": published_at,
            "filing_status": "original"}


def test_fundamental_node_filters_future_and_undated_facts():
    state = {"request": {"symbol": "INFY", "as_of": "2025-05-01T00:00:00+05:30"},
             "fundamental_data": {"facts": [
                 _fact("REVENUE", 100, "2024-12-31", "2025-01-20T10:00:00+05:30"),
                 _fact("REVENUE", 999, "2025-03-31", "2025-05-20T10:00:00+05:30"),
                 {"symbol": "INFY", "metric_code": "NET_INCOME", "value": 4},
             ]}}
    result = analyze_fundamentals(state)
    report = result["fundamental_analysis"]
    assert report["data_quality"]["facts_used"] == 1
    assert report["data_quality"]["excluded_future"] == 1
    assert report["data_quality"]["excluded_undated"] == 1
    assert all(item.get("value") != 999 for item in report["findings"])
    assert result["workflow_trace"] == ["fundamental_analyst"]


def test_fundamental_node_handles_no_facts_without_fabricating():
    result = analyze_fundamentals({"request": {"symbol": "INFY", "as_of": "2025-05-01T00:00:00Z"},
                                   "fundamental_data": {"facts": []}})
    report = result["fundamental_analysis"]
    assert report["status"] == "unavailable"
    assert report["confidence"] == 0.0
    assert report["findings"] == []
    assert report["missing_data"]


def test_interpreter_can_add_narrative_but_not_replace_numeric_findings():
    state = {"request": {"symbol": "INFY", "as_of": "2025-05-01T00:00:00Z"},
             "fundamental_data": {"facts": [_fact("REVENUE", 100, "2024-12-31", "2025-01-20T10:00:00Z")]}}
    result = analyze_fundamentals(state, interpreter=lambda _: {"summary": "Revenue fact found.", "findings": [{"value": 999}]})
    report = result["fundamental_analysis"]
    assert report["summary"] == "Revenue fact found."
    assert report["findings"][0]["value"] == 100

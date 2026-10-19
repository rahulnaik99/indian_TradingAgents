from tradingagents.web.app import _research_qa_compact_analysis


def test_oversized_analysis_is_compacted_under_budget():
    analysis = {
        "symbol": "CANBK",
        "run_id": "run-1",
        "as_of": "2026-10-15T10:00:00+05:30",
        "final_agent_rating": "REVIEW",
        "final_conclusion": "A concise conclusion",
        "summary": "summary " * 1000,
        "technical": {"raw_report": "raw " * 10000, "ema20": 100, "ema50": 98, "history": ["candle"] * 10000},
        "evidence_package": {
            "facts": [{"name": f"fact-{i}", "value": i, "description": "x" * 2000} for i in range(200)],
            "events": [{"title": "event", "description": "y" * 1000} for _ in range(100)],
            "calendar_events": [{"title": "calendar", "description": "z" * 1000} for _ in range(100)],
            "web_research": {"sources": [{"title": "source", "url": "https://example.com", "text": "q" * 2000} for _ in range(100)]},
            "technical": {"raw_report": "r" * 100000, "ema20": 100},
        },
        "data_sources": {"huge": "s" * 100000},
    }
    compact, serialized = _research_qa_compact_analysis(analysis, "CANBK")
    assert len(serialized) <= 16000
    assert compact["symbol"] == "CANBK"
    assert compact["final_agent_rating"] == "REVIEW"
    assert "raw_report" not in str(compact)


def test_small_analysis_preserves_useful_evidence():
    analysis = {
        "symbol": "CANBK", "run_id": "run-2", "as_of": "2026-10-15T10:00:00+05:30",
        "final_agent_rating": "BUY", "final_conclusion": "Trend is positive",
        "technical": {"ema20": 101, "ema50": 99, "rsi": 58},
        "evidence_package": {"facts": [{"name": "ROE", "value": 14}], "events": [{"title": "Results"}]},
    }
    compact, serialized = _research_qa_compact_analysis(analysis, "CANBK")
    assert len(serialized) <= 16000
    assert compact["final_conclusion"] == "Trend is positive"
    assert compact["technical"]["ema20"] == 101

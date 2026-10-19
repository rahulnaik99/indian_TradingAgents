from tradingagents.web.app import _research_qa_thesis_comparison


def test_bullish_thesis_is_weakened_by_negative_daily_move():
    analysis = {
        "symbol": "RELIANCE",
        "final_agent_rating": "BUY",
        "summary": "Bullish outlook based on available evidence",
    }
    evidence = {
        "market_data": {
            "latest": {"date": "2026-10-15", "close": 100},
            "change_pct": -2.1,
            "technical_snapshot": {"ema20": 105, "ema50": 108},
        }
    }
    result = _research_qa_thesis_comparison(analysis, evidence, [
        {"tool": "market_price_history", "status": "completed"}
    ])
    assert result["original_direction"] == "bullish"
    assert result["overall_assessment"] == "thesis_weakened_by_available_signals"
    assert any(x["evidence"] == "recent_daily_price_change" and x["classification"] == "weakens"
               for x in result["findings"])
    assert result["confidence"] == "low_to_moderate"


def test_mixed_price_and_news_evidence_stays_mixed():
    analysis = {"final_agent_rating": "BUY"}
    evidence = {
        "market_data": {
            "latest": {"date": "2026-10-15", "close": 120},
            "change_pct": 2.0,
            "technical_snapshot": {"ema20": 115, "ema50": 110},
        },
        "latest_news": {"events": [
            {"direction": "negative", "title": "Negative headline"},
            {"direction": "negative", "title": "Another negative headline"},
        ]},
    }
    result = _research_qa_thesis_comparison(analysis, evidence, [
        {"tool": "market_price_history", "status": "completed"},
        {"tool": "search_latest_news", "status": "completed"},
    ])
    assert result["overall_assessment"] == "mixed_evidence"
    assert any(x["classification"] == "supports" for x in result["findings"])
    assert any(x["classification"] == "weakens" for x in result["findings"])


def test_unclear_original_thesis_does_not_force_direction():
    result = _research_qa_thesis_comparison(
        {"summary": "The evidence is mixed and no clear conclusion is available."},
        {"market_data": {"latest": {"date": "2026-10-15", "close": 100}, "change_pct": -3}},
        [{"tool": "market_price_history", "status": "completed"}],
    )
    assert result["original_direction"] == "mixed_or_unclear"
    assert result["overall_assessment"] == "insufficient_evidence"
    assert result["confidence"] == "low"


def test_tool_failure_marks_comparison_incomplete():
    result = _research_qa_thesis_comparison(
        {"final_agent_rating": "SELL"}, {},
        [{"tool": "search_latest_news", "status": "failed"}],
    )
    assert result["overall_assessment"] == "incomplete_retrieval"
    assert result["failed_tools"] == ["search_latest_news"]
    assert result["confidence"] == "low"

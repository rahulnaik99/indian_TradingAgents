from tradingagents.web.app import _classify_research_qa_intent


def test_report_question_does_not_select_live_tools():
    plan = _classify_research_qa_intent("Why did the report reach a bearish conclusion?")
    assert plan["intent"] == "completed_analysis"
    assert plan["requested_tools"] == []
    assert plan["uses_original_analysis"] is True
    assert plan["read_only"] is True


def test_latest_price_selects_market_history():
    plan = _classify_research_qa_intent("What is the latest price?")
    assert plan["intent"] == "live_retrieval"
    assert "market_price_history" in plan["requested_tools"]
    assert plan["explicit_freshness_requested"] is True


def test_news_and_corporate_event_can_be_combined():
    plan = _classify_research_qa_intent("What is the latest news and upcoming dividend?")
    assert "search_latest_news" in plan["requested_tools"]
    assert "get_corporate_events" in plan["requested_tools"]


def test_thesis_comparison_selects_market_and_news():
    plan = _classify_research_qa_intent("Has new information changed the thesis?")
    assert plan["intent"] == "thesis_comparison"
    assert "market_price_history" in plan["requested_tools"]
    assert "search_latest_news" in plan["requested_tools"]

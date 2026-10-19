from datetime import datetime, timezone

import pytest

from tradingagents.india.events import classify_event
from tradingagents.india.fundamentals import FundamentalDataError, import_facts_csv, parse_xbrl_facts
from tradingagents.india.moneycontrol import MoneycontrolPermissionError, fetch_statement_tables
from tradingagents.india.paper_trading import PaperBroker, PaperTradingError
from tradingagents.india.storage import IndiaStore


def test_event_classifier_marks_rbi_rate_as_high_impact_uncertain():
    result = classify_event("RBI raises repo rate by 25 bps", url="https://www.rbi.org.in/example")
    assert result["event_type"] == "monetary_policy"
    assert result["severity"] == "high"
    assert result["is_official_source"] is True


def test_financial_csv_import_requires_publication_timestamp_and_persists(tmp_path):
    store = IndiaStore(tmp_path / "facts.sqlite3")
    csv_text = "symbol,metric_code,value,unit,period_start,period_end,period_type,scope,source_name,source_url,published_at\nSBIN,net_profit,100,INR_crore,2026-04-01,2026-06-30,quarter,consolidated,NSE,https://nseindia.com/test,2026-08-01T10:00:00+05:30\n"
    result = import_facts_csv(csv_text, store)
    assert result["inserted"] == 1
    assert store.list_facts("SBIN")[0]["metric_code"] == "net_profit"
    with pytest.raises(FundamentalDataError):
        import_facts_csv("symbol,value\nSBIN,1\n", store)


def test_xbrl_parser_preserves_original_taxonomy_and_period():
    xml = '''<xbrl xmlns="http://www.xbrl.org/2003/instance"><context id="q1"><entity><identifier scheme="x">SBIN</identifier></entity><period><startDate>2026-04-01</startDate><endDate>2026-06-30</endDate></period></context><ns:ProfitLoss xmlns:ns="http://example.com" contextRef="q1">123.4</ns:ProfitLoss></xbrl>'''
    result = parse_xbrl_facts(xml, symbol="SBIN", source_url="https://nseindia.com/file.xml", published_at="2026-08-01T10:00:00+05:30")
    assert result["fact_count"] == 1
    assert result["facts"][0]["reported_metric_name"] == "ProfitLoss"
    assert result["facts"][0]["mapping_status"] == "unmapped_taxonomy_fact"


def test_moneycontrol_is_blocked_without_permission(monkeypatch):
    monkeypatch.delenv("MONEYCONTROL_PERMISSION_CONFIRMED", raising=False)
    with pytest.raises(MoneycontrolPermissionError):
        fetch_statement_tables("https://www.moneycontrol.com/financials/example")


def test_paper_broker_fills_simulated_order_and_prevents_oversell(tmp_path):
    store = IndiaStore(tmp_path / "paper.sqlite3")
    broker = PaperBroker(store)
    order = broker.place_order(symbol="SBIN", side="buy", quantity=10, reference_price=100, stop_loss=95, target=110)
    assert order["status"] == "PAPER_FILLED"
    assert order["live_order"] is False
    with pytest.raises(PaperTradingError):
        broker.place_order(symbol="SBIN", side="sell", quantity=11, reference_price=105)
    broker.place_order(symbol="SBIN", side="sell", quantity=4, reference_price=105)
    portfolio = broker.portfolio()
    assert portfolio["positions"]["SBIN"]["quantity"] == 6
    assert portfolio["realized_pnl_before_costs"] == 20


def test_web_research_rss_fallback_classifies_and_preserves_link():
    from tradingagents.india.events import WebResearchService

    class FakeResponse:
        ok = True
        status_code = 200
        content = b'''<rss><channel><item><title>RBI raises repo rate</title><link>https://rbi.org.in/policy</link><description>Policy announcement</description><pubDate>Fri, 09 Oct 2026 10:00:00 +0530</pubDate><source>RBI</source></item></channel></rss>'''
        def raise_for_status(self):
            return None
        def json(self):
            return {"results": []}

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()
        def get(self, *args, **kwargs):
            return FakeResponse()

    results = WebResearchService(session=FakeSession()).search("RBI repo rate")
    assert len(results) == 1
    assert results[0]["event_type"] == "monetary_policy"
    assert results[0]["source_url"] == "https://rbi.org.in/policy"


def test_moneycontrol_parser_returns_raw_table_rows_only_when_permitted(monkeypatch):
    from tradingagents.india.moneycontrol import fetch_statement_tables

    monkeypatch.setenv("MONEYCONTROL_PERMISSION_CONFIRMED", "true")

    class FakeResponse:
        text = "<html><table><tr><th>Metric</th><th>FY26</th></tr><tr><td>Net Profit</td><td>123</td></tr></table></html>"
        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    result = fetch_statement_tables("https://www.moneycontrol.com/financials/test", session=FakeSession())
    assert result["table_count"] == 1
    assert result["tables"][0][1] == ["Net Profit", "123"]


def test_financial_summary_uses_published_time_and_computes_qoq():
    from tradingagents.india.fundamentals import summarize_facts

    facts = []
    for period_end, value, published in [
        ("2026-03-31", 100.0, "2026-05-01T10:00:00+05:30"),
        ("2026-06-30", 110.0, "2026-08-01T10:00:00+05:30"),
        ("2026-09-30", 120.0, "2026-11-01T10:00:00+05:30"),
    ]:
        facts.append({"symbol": "SBIN", "metric_code": "net_profit", "value": value, "unit": "INR_crore",
                      "currency": "INR", "period_start": "2026-01-01", "period_end": period_end,
                      "period_type": "quarter", "scope": "consolidated", "source_name": "NSE",
                      "source_url": "https://nseindia.com/test", "published_at": published,
                      "retrieved_at": published, "filing_status": "original"})
    result = summarize_facts(facts, as_of=__import__("datetime").datetime.fromisoformat("2026-09-15T12:00:00+05:30"))
    assert result["facts_count"] == 2
    current = next(item for item in result["facts"] if item["period_end"] == "2026-06-30")
    assert current["qoq"]["percentage_change"] == 10.0


def test_same_event_can_be_linked_to_multiple_symbols(tmp_path):
    store = IndiaStore(tmp_path / "events.sqlite3")
    base = {"event_id": "shared", "event_type": "government_policy", "title": "Banking policy",
            "summary": "Sector-wide", "source_name": "Govt", "source_url": "https://gov.in/policy",
            "published_at": "2026-10-01T10:00:00+05:30", "retrieved_at": "2026-10-01T10:01:00+05:30",
            "severity": "medium", "direction": "uncertain", "confidence": 0.6}
    store.save_event({**base, "symbol": "SBIN"})
    store.save_event({**base, "symbol": "ICICIBANK"})
    assert len(store.list_events("SBIN")) == 1
    assert len(store.list_events("ICICIBANK")) == 1


def test_wigolo_search_normalizes_results_and_publication_date():
    from tradingagents.india.events import WebResearchService

    class FakeResponse:
        ok = True
        def raise_for_status(self):
            return None
        def json(self):
            return {"results": [{"title": "Canara Bank quarterly results", "url": "https://www.nseindia.com/example",
                                  "snippet": "Net profit reported in filing", "freshness_signal": {"published_date": "2026-10-09"}}]}

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()
        def get(self, *args, **kwargs):
            raise AssertionError("RSS fallback should not be used when Wigolo returned results")

    results = WebResearchService(session=FakeSession()).search("Canara Bank results")
    assert results[0]["provider"] == "wigolo"
    assert results[0]["source_url"] == "https://www.nseindia.com/example"
    assert results[0]["published_at"] == "2026-10-09"
    assert results[0]["is_official_source"] is True


def test_wigolo_research_returns_structured_brief():
    from tradingagents.india.events import WebResearchService

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {"brief": {"key_findings": ["Example sourced finding"]}, "sources": [{"url": "https://www.rbi.org.in/example"}]}

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()

    result = WebResearchService(session=FakeSession()).research("Research RBI policy", depth="quick", max_sources=5)
    assert result["source"] == "wigolo"
    assert result["result"]["brief"]["key_findings"] == ["Example sourced finding"]

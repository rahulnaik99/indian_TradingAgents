from tradingagents.india.agent_tool_adapter import india_tool_result, use_india_evidence


def sample_package():
    return {
        "schema_version": "1.0", "symbol": "SBIN", "exchange": "NSE",
        "as_of": "2026-10-09T15:30:00+05:30",
        "market_data": {"provider": "fyers", "technical_snapshot": {
            "multi_timeframe_bias": "bullish_alignment",
            "timeframes": {"1d": {"status": "ok", "count": 250, "ema20": 800.0, "rsi14": 62.0, "latest_close": 805.0}}
        }},
        "news_and_events": [{"published_at": "2026-10-08T10:00:00+05:30", "published_at_utc": "2026-10-08T04:30:00+00:00",
            "title": "Company files quarterly results", "summary": "Results filed", "event_type": "quarterly_results",
            "severity": "high", "direction": "uncertain", "source_url": "https://www.nseindia.com/test"}],
        "financial_facts": [{"metric_code": "net_profit", "value": 123.0, "unit": "INR_crore", "currency": "INR",
            "period_start": "2026-04-01", "period_end": "2026-06-30", "period_type": "quarter", "scope": "consolidated",
            "published_at": "2026-08-01T10:00:00+05:30", "published_at_utc": "2026-08-01T04:30:00+00:00",
            "source_name": "NSE", "source_url": "https://www.nseindia.com/fact"}],
    }


def test_india_adapter_routes_news_to_dated_shared_events():
    with use_india_evidence(sample_package()):
        result = india_tool_result("get_news", "NSE:SBIN-EQ", "2026-10-01", "2026-10-09")
    assert "Company files quarterly results" in result
    assert "https://www.nseindia.com/test" in result
    assert "2026-10-08" in result


def test_india_adapter_routes_fundamentals_with_period_unit_and_source():
    with use_india_evidence(sample_package()):
        result = india_tool_result("get_fundamentals", "NSE:SBIN-EQ", "2026-10-09")
    assert "net_profit" in result
    assert "INR_crore" in result
    assert "2026-04-01..2026-06-30" in result
    assert "https://www.nseindia.com/fact" in result


def test_india_adapter_routes_indicators_to_shared_snapshot():
    with use_india_evidence(sample_package()):
        result = india_tool_result("get_indicators", "NSE:SBIN-EQ", "rsi", "2026-10-09", 30)
    assert "rsi14=62.0" in result
    assert "provider=fyers" in result


def test_india_adapter_fails_closed_for_unsupported_insider_tool():
    with use_india_evidence(sample_package()):
        result = india_tool_result("get_insider_transactions", "NSE:SBIN-EQ")
    assert result.startswith("INDIA_DATA_UNAVAILABLE")
    assert "no point-in-time Indian insider-transaction adapter" in result


def test_non_india_graph_keeps_upstream_tool_behavior():
    assert india_tool_result("get_news", "AAPL", "2026-10-01", "2026-10-09") is None


def test_india_news_adapter_includes_only_eligible_research_citations():
    package = sample_package()
    package["web_research"] = {
        "provider": "wigolo", "brief": "Dated brief: official quarterly filing was published.",
        "sources": [{"title": "NSE filing", "url": "https://www.nseindia.com/filing",
                     "published_at": "2026-10-08T11:00:00+05:30",
                     "published_at_utc": "2026-10-08T05:30:00+00:00"}],
    }
    with use_india_evidence(package):
        result = india_tool_result("get_news", "NSE:SBIN-EQ", "2026-10-01", "2026-10-09")
    assert "Dated brief" in result
    assert "https://www.nseindia.com/filing" in result


def test_india_candle_adapter_does_not_fallback_when_fyers_is_unconfigured(monkeypatch):
    monkeypatch.delenv("FYERS_APP_ID", raising=False)
    monkeypatch.delenv("FYERS_ACCESS_TOKEN", raising=False)
    with use_india_evidence(sample_package()):
        result = india_tool_result("get_stock_data", "NSE:SBIN-EQ", "2026-09-01", "2026-10-09")
    assert result.startswith("INDIA_DATA_UNAVAILABLE [get_stock_data]")
    assert "no alternate market-data provider was silently substituted" in result.lower()

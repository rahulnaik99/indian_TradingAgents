from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tradingagents.india.contracts import OHLCVCandle, SwingTimeframe
from tradingagents.india.langgraph_pipeline.data_collection import EvidenceCollector, collect_evidence

IST = ZoneInfo("Asia/Kolkata")


class FakeMarketClient:
    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    def get_history(self, symbol, start_date, end_date, timeframe):
        self.calls.append((symbol, start_date, end_date, timeframe))
        return tuple(c for c in self.candles if c.timeframe.value == timeframe)


class FakeStore:
    def list_facts(self, symbol, limit=500):
        return [
            {"symbol": symbol, "metric_code": "revenue", "published_at": "2026-10-01T10:00:00+05:30", "value": 10},
            {"symbol": symbol, "metric_code": "future", "published_at": "2026-10-20T10:00:00+05:30", "value": 99},
            {"symbol": symbol, "metric_code": "unknown", "value": 88},
        ]

    def list_events(self, symbol, limit=100):
        return [{"symbol": symbol, "title": "Old news", "published_at": "2026-10-01T10:00:00+05:30"},
                {"symbol": symbol, "title": "Future news", "published_at": "2026-10-20T10:00:00+05:30"}]

    def list_calendar_events(self, start, end):
        return [{"title": "Known event", "event_date": "2026-10-25", "created_at": "2026-10-01T10:00:00+05:30"},
                {"title": "Unknown known-at", "event_date": "2026-10-25"}]


def candle(tf, open_time, close_time, complete=True):
    return OHLCVCandle(symbol="ABC", timeframe=SwingTimeframe(tf), open_time=open_time, close_time=close_time,
                       open=100, high=105, low=95, close=102, volume=1000, source_provider="test",
                       retrieved_at=datetime(2026, 10, 18, 10, tzinfo=IST), is_complete=complete)


def test_collection_filters_future_and_incomplete_candles():
    cutoff = datetime(2026, 10, 18, 10, 0, tzinfo=IST)
    client = FakeMarketClient([
        candle("1d", cutoff - timedelta(days=1), cutoff - timedelta(hours=1)),
        candle("1d", cutoff - timedelta(minutes=30), cutoff + timedelta(minutes=30)),
        candle("1d", cutoff - timedelta(days=2), cutoff - timedelta(days=1), complete=False),
    ])
    result = EvidenceCollector(market_client=client, store=FakeStore(), clock=lambda: cutoff).collect({
        "symbol": "abc", "as_of": cutoff.isoformat(), "timeframes": ["1d"]
    })
    assert result["request"]["symbol"] == "ABC"
    assert len(result["market_data"]["candles_by_timeframe"]["1d"]) == 1
    assert len(result["fundamental_data"]["facts"]) == 1
    assert [e["title"] for e in result["events_data"]] == ["Old news", "Known event"]
    assert result["evidence_metadata"]["financial_fact_count"] == 1


def test_node_wrapper_requires_request():
    try:
        collect_evidence({})
    except ValueError as exc:
        assert "state.request" in str(exc)
    else:
        raise AssertionError("expected request validation")

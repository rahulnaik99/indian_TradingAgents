from datetime import datetime, timedelta, timezone

from tradingagents.india.langgraph_pipeline.agents.technical_analyst.agent import analyze_technicals


def _row(symbol, timeframe, index, cutoff):
    opened = cutoff - timedelta(days=(220 - index))
    close = 100 + index * 0.2
    return {
        "symbol": symbol, "timeframe": timeframe,
        "open_time": opened.isoformat(),
        "close_time": (opened + timedelta(days=1)).isoformat(),
        "open": close - 0.1, "high": close + 0.5,
        "low": close - 0.5, "close": close, "volume": 1000 + index,
        "source_provider": "test", "retrieved_at": cutoff.isoformat(),
        "is_complete": True, "quality_flags": [],
    }


def test_empty_market_data_returns_unavailable_not_crash():
    cutoff = datetime(2026, 10, 18, tzinfo=timezone.utc)
    result = analyze_technicals({"request": {"symbol": "TEST", "as_of": cutoff.isoformat(),
                                             "timeframes": ["1d", "4h", "1h"]},
                                 "market_data": {"candles_by_timeframe": {}}})
    assert result["technical_analysis"]["status"] == "unavailable"
    assert result["technical_data"]["multi_timeframe_bias"] == "insufficient_data"


def test_analyst_excludes_future_and_incomplete_candles():
    cutoff = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    rows = []
    for i in range(220):
        row = _row("TEST", "1d", i, cutoff)
        # Make the last generated row close after cutoff; it must not be used.
        if i == 219:
            row["close_time"] = (cutoff + timedelta(hours=1)).isoformat()
        rows.append(row)
    incomplete = dict(_row("TEST", "1d", 218, cutoff))
    incomplete["is_complete"] = False
    rows.append(incomplete)
    result = analyze_technicals({"request": {"symbol": "TEST", "as_of": cutoff.isoformat(),
                                             "timeframes": ["1d"]},
                                 "market_data": {"candles_by_timeframe": {"1d": rows}}})
    quality = result["technical_analysis"]["data_quality"]
    assert quality["excluded_future_candles"] == 1
    assert quality["excluded_incomplete_candles"] == 1
    assert quality["candle_counts"]["1d"] == 219
    assert "technical_analyst" in result["workflow_trace"]


def test_invalid_request_timestamp_is_rejected():
    try:
        analyze_technicals({"request": {"symbol": "TEST", "as_of": "2026-10-18T12:00:00",
                                         "timeframes": ["1d"]}})
    except ValueError as exc:
        assert "timezone-aware" in str(exc)
    else:
        raise AssertionError("naive timestamp should be rejected")

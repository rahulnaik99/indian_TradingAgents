from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tradingagents.india.contracts import OHLCVCandle, SwingTimeframe
from tradingagents.india.technical import analyze_multi_timeframe, analyze_timeframe

TZ = ZoneInfo("Asia/Kolkata")


def candles(symbol: str, tf: SwingTimeframe, count: int, step: timedelta, *, start=None):
    start = start or datetime(2025, 1, 1, 9, 15, tzinfo=TZ)
    rows = []
    price = 100.0
    for i in range(count):
        price += 0.3
        op = price - 0.1
        close = price
        rows.append(OHLCVCandle(symbol=symbol, timeframe=tf, open_time=start + i * step,
            close_time=start + i * step + step, open=op, high=price + 0.5, low=op - 0.5,
            close=close, volume=1000 + i, source_provider="test", retrieved_at=start + i * step + step,
            is_complete=True))
    return rows


def test_timeframe_analysis_computes_indicators_and_levels():
    result = analyze_timeframe(candles("SBIN", SwingTimeframe.DAILY, 230, timedelta(days=1)), "1d")
    assert result["status"] == "ok"
    assert result["ema200"] is not None
    assert result["atr14"] > 0
    assert result["trend"] == "bullish"
    assert result["support_20"] < result["resistance_20"]


def test_multi_timeframe_requires_alignment_and_never_claims_order_placed():
    series = {
        "1d": candles("SBIN", SwingTimeframe.DAILY, 230, timedelta(days=1)),
        "4h": candles("SBIN", SwingTimeframe.FOUR_HOUR, 230, timedelta(hours=4)),
        "1h": candles("SBIN", SwingTimeframe.HOURLY, 230, timedelta(hours=1)),
    }
    result = analyze_multi_timeframe(series, symbol="SBIN")
    assert result["multi_timeframe_bias"] == "bullish_alignment"
    assert result["candidate_setup"]["direction"] == "long_watchlist_only"
    assert "not a trade instruction" in result["disclaimer"]

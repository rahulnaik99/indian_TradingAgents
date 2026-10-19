"""Deterministic multi-timeframe technical analysis for Indian swing trading.

This module does not call an LLM. Indicators and risk arithmetic are computed
from validated OHLCV candles so the LLM cannot invent numeric values.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tradingagents.india.contracts import OHLCVCandle, SwingTimeframe
from tradingagents.india.tracing import traced


def _ema(values: list[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    current = seed
    for i in range(period, len(values)):
        current = values[i] * alpha + current * (1.0 - alpha)
        out[i] = current
    return out


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(v, 0.0) for v in changes]
    losses = [max(-v, 0.0) for v in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(candles: list[OHLCVCandle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    tr: list[float] = []
    for i, candle in enumerate(candles):
        if i == 0:
            tr.append(candle.high - candle.low)
        else:
            prev = candles[i - 1].close
            tr.append(max(candle.high - candle.low, abs(candle.high - prev), abs(candle.low - prev)))
    value = sum(tr[1 : period + 1]) / period
    for current in tr[period + 1 :]:
        value = (value * (period - 1) + current) / period
    return value


def _vwap(candles: list[OHLCVCandle]) -> float | None:
    if not candles:
        return None
    # A session VWAP is calculated only from the latest exchange-local session.
    latest_day = candles[-1].open_time.date()
    session = [c for c in candles if c.open_time.date() == latest_day]
    total_volume = sum(c.volume for c in session)
    if total_volume <= 0:
        return None
    return sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in session) / total_volume


def _market_structure(candles: list[OHLCVCandle], lookback: int = 20) -> dict[str, Any]:
    recent = candles[-lookback:]
    if len(recent) < 5:
        return {"label": "insufficient_data", "higher_highs": None, "higher_lows": None}
    midpoint = max(2, len(recent) // 2)
    first, second = recent[:midpoint], recent[midpoint:]
    high_1, high_2 = max(c.high for c in first), max(c.high for c in second)
    low_1, low_2 = min(c.low for c in first), min(c.low for c in second)
    hh, hl = high_2 > high_1, low_2 > low_1
    lh, ll = high_2 < high_1, low_2 < low_1
    if hh and hl:
        label = "higher_highs_higher_lows"
    elif lh and ll:
        label = "lower_highs_lower_lows"
    elif hh or hl:
        label = "potential_bullish_structure"
    elif lh or ll:
        label = "potential_bearish_structure"
    else:
        label = "range_or_mixed"
    return {"label": label, "higher_highs": hh, "higher_lows": hl,
            "lower_highs": lh, "lower_lows": ll,
            "prior_segment_high": round(high_1, 4), "recent_segment_high": round(high_2, 4),
            "prior_segment_low": round(low_1, 4), "recent_segment_low": round(low_2, 4)}


@traced("india.technical.analyze_timeframe")
def _fibonacci(candles: list[OHLCVCandle], lookback: int = 60) -> dict[str, float] | None:
    recent = candles[-lookback:]
    if len(recent) < 5:
        return None
    swing_low = min(c.low for c in recent)
    swing_high = max(c.high for c in recent)
    distance = swing_high - swing_low
    if distance <= 0:
        return None
    return {
        "swing_low": round(swing_low, 4), "swing_high": round(swing_high, 4),
        "23.6%": round(swing_high - distance * 0.236, 4),
        "38.2%": round(swing_high - distance * 0.382, 4),
        "50.0%": round(swing_high - distance * 0.5, 4),
        "61.8%": round(swing_high - distance * 0.618, 4),
        "78.6%": round(swing_high - distance * 0.786, 4),
    }


def analyze_timeframe(candles: Iterable[OHLCVCandle], timeframe: str) -> dict[str, Any]:
    """Compute trend, EMA, RSI, ATR, VWAP and support/resistance for one timeframe."""
    data = sorted((c for c in candles if c.is_complete), key=lambda c: c.open_time)
    if not data:
        return {"timeframe": timeframe, "status": "no_complete_candles", "count": 0}
    closes = [float(c.close) for c in data]
    ema20, ema50, ema100, ema200 = (_ema(closes, p) for p in (20, 50, 100, 200))
    last = data[-1]
    current_ema20, current_ema50 = ema20[-1], ema50[-1]
    current_ema100, current_ema200 = ema100[-1], ema200[-1]
    votes = []
    if current_ema20 is not None and current_ema50 is not None:
        votes.append(1 if current_ema20 > current_ema50 else -1 if current_ema20 < current_ema50 else 0)
    if current_ema100 is not None:
        votes.append(1 if last.close > current_ema100 else -1 if last.close < current_ema100 else 0)
    if current_ema200 is not None:
        votes.append(1 if last.close > current_ema200 else -1 if last.close < current_ema200 else 0)
    score = sum(votes) / len(votes) if votes else 0.0
    trend = "bullish" if score >= 0.34 else "bearish" if score <= -0.34 else "neutral_or_insufficient"
    recent = data[-20:]
    support = min(c.low for c in recent)
    resistance = max(c.high for c in recent)
    previous_day = data[-2] if len(data) >= 2 else None
    avg_volume = sum(c.volume for c in data[-20:]) / min(20, len(data))
    relative_volume = (last.volume / avg_volume) if avg_volume > 0 else None
    return {
        "timeframe": timeframe,
        "status": "ok" if len(data) >= 20 else "limited_history",
        "count": len(data),
        "latest_open_time": last.open_time.isoformat(),
        "latest_close_time": last.close_time.isoformat(),
        "latest_close": round(last.close, 4),
        "trend": trend,
        "trend_score": round(score, 3),
        "ema20": round(current_ema20, 4) if current_ema20 is not None else None,
        "ema50": round(current_ema50, 4) if current_ema50 is not None else None,
        "ema100": round(current_ema100, 4) if current_ema100 is not None else None,
        "ema200": round(current_ema200, 4) if current_ema200 is not None else None,
        "rsi14": round(_rsi(closes), 2) if _rsi(closes) is not None else None,
        "atr14": round(_atr(data), 4) if _atr(data) is not None else None,
        "session_vwap": round(_vwap(data), 4) if timeframe in {"15m", "1h"} and _vwap(data) is not None else None,
        "support_20": round(support, 4),
        "resistance_20": round(resistance, 4),
        "previous_candle_high": round(previous_day.high, 4) if previous_day else None,
        "previous_candle_low": round(previous_day.low, 4) if previous_day else None,
        "volume_latest": last.volume,
        "volume_average_20": round(avg_volume, 2),
        "relative_volume_20": round(relative_volume, 3) if relative_volume is not None else None,
        "fibonacci_retracement_60": _fibonacci(data),
        "market_structure": _market_structure(data),
        "is_complete_candle_only": True,
    }


@traced("india.technical.analyze_multi_timeframe")
def analyze_multi_timeframe(series: dict[str, Iterable[OHLCVCandle]], *, symbol: str) -> dict[str, Any]:
    """Combine 1D, 4H, 1H and optional 15m analysis without forcing a trade."""
    expected = ("1d", "4h", "1h")
    reports = {tf: analyze_timeframe(series.get(tf, ()), tf) for tf in (*expected, "15m") if tf in series or tf in expected}
    usable = [reports[tf] for tf in expected if reports[tf].get("trend") in {"bullish", "bearish"}]
    score = sum(float(item["trend_score"]) for item in usable) / len(usable) if usable else 0.0
    trends = [item["trend"] for item in usable]
    if len(usable) < 2:
        bias = "insufficient_data"
    elif all(t == "bullish" for t in trends):
        bias = "bullish_alignment"
    elif all(t == "bearish" for t in trends):
        bias = "bearish_alignment"
    elif score > 0.25:
        bias = "mixed_bullish_bias"
    elif score < -0.25:
        bias = "mixed_bearish_bias"
    else:
        bias = "mixed_or_neutral"
    # Research-only long setup; not an order. Only compute levels if daily ATR exists.
    daily = reports.get("1d", {})
    last_price = daily.get("latest_close")
    atr = daily.get("atr14")
    setup = {"status": "no_setup", "direction": "none", "reason": "Timeframe alignment or history is insufficient."}
    if bias == "bullish_alignment" and last_price is not None and atr is not None and atr > 0:
        stop = round(last_price - 1.5 * atr, 4)
        target = round(last_price + 3.0 * atr, 4)
        setup = {"status": "candidate_for_review", "direction": "long_watchlist_only",
                 "reference_close": last_price, "illustrative_stop": stop,
                 "illustrative_target": target, "risk_reward": 2.0,
                 "reason": "Daily, 4H and 1H trends align bullish; confirm liquidity, event risk and resistance before any decision."}
    return {"symbol": symbol.upper(), "strategy": "swing_trading", "timeframes": reports,
            "aggregate_trend_score": round(score, 3), "multi_timeframe_bias": bias,
            "candidate_setup": setup,
            "disclaimer": "Research output only; not a trade instruction. Indicators need sufficient history and must be validated against market data quality."}

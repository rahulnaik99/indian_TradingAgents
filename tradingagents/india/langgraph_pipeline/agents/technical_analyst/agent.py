"""Technical analyst node for the new LangGraph trading-analysis pipeline.

All price indicators are calculated by the existing deterministic technical
module. This node only interprets validated candle data and emits state updates;
it does not fetch market data or place orders.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from tradingagents.india.contracts import OHLCVCandle, SwingTimeframe
from tradingagents.india.technical import analyze_multi_timeframe


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _to_candle(row: dict[str, Any], symbol: str, timeframe: str) -> OHLCVCandle | None:
    """Convert a serialized candle into the provider-neutral validated contract."""
    try:
        open_time = _parse_timestamp(row.get("open_time"))
        close_time = _parse_timestamp(row.get("close_time"))
        retrieved_at = _parse_timestamp(row.get("retrieved_at"))
        if not open_time or not close_time or not retrieved_at:
            return None
        return OHLCVCandle(
            symbol=str(row.get("symbol") or symbol).upper(),
            timeframe=SwingTimeframe(timeframe),
            open_time=open_time,
            close_time=close_time,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]),
            source_provider=str(row.get("source_provider") or "unknown"),
            retrieved_at=retrieved_at,
            is_complete=bool(row.get("is_complete", False)),
            is_adjusted=row.get("is_adjusted"),
            quality_flags=tuple(row.get("quality_flags") or ()),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def analyze_technicals(
    state: dict[str, Any],
    *,
    interpreter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """LangGraph-compatible Technical Analyst node.

    Revalidates point-in-time constraints at the analyst boundary, converts the
    collector's JSON candle records to ``OHLCVCandle`` contracts, calculates
    multi-timeframe indicators, and emits ``technical_data`` and
    ``technical_analysis``. Optional interpretation can add narrative only.
    """
    request = state.get("request")
    if not isinstance(request, dict):
        raise ValueError("state.request is required")
    symbol = request.get("symbol")
    cutoff = _parse_timestamp(request.get("as_of"))
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("request.symbol is required")
    if cutoff is None:
        raise ValueError("request.as_of must be timezone-aware")

    market = state.get("market_data") or {}
    raw_series = market.get("candles_by_timeframe", {}) if isinstance(market, dict) else {}
    if not isinstance(raw_series, dict):
        raw_series = {}

    series: dict[str, list[OHLCVCandle]] = {}
    excluded_future = 0
    excluded_incomplete = 0
    excluded_invalid = 0
    counts: dict[str, int] = {}
    requested = request.get("timeframes") or ["1d", "4h", "1h"]
    supported = {item.value for item in SwingTimeframe}
    timeframes = [tf for tf in requested if tf in supported]

    for timeframe in timeframes:
        rows = raw_series.get(timeframe, [])
        if not isinstance(rows, list):
            rows = []
        candles: list[OHLCVCandle] = []
        for row in rows:
            if not isinstance(row, dict):
                excluded_invalid += 1
                continue
            close_time = _parse_timestamp(row.get("close_time"))
            if close_time is None:
                excluded_invalid += 1
                continue
            if close_time > cutoff:
                excluded_future += 1
                continue
            if row.get("is_complete") is not True:
                excluded_incomplete += 1
                continue
            candle = _to_candle(row, symbol, timeframe)
            if candle is None:
                excluded_invalid += 1
                continue
            candles.append(candle)
        series[timeframe] = sorted(candles, key=lambda item: item.open_time)
        counts[timeframe] = len(series[timeframe])

    # Existing analyzer expects daily, 4h, 1h, and optional 15m keys.
    analysis = analyze_multi_timeframe(series, symbol=symbol.upper())
    timeframe_reports = analysis.get("timeframes", {})
    usable = [v for v in timeframe_reports.values() if v.get("status") in {"ok", "limited_history"}]
    if not any(counts.values()):
        status = "unavailable"
        missing = [f"{tf} candles" for tf in timeframes or ["1d", "4h", "1h"]]
        confidence = 0.0
    elif analysis.get("multi_timeframe_bias") == "insufficient_data":
        status = "limited"
        missing = [tf for tf in ("1d", "4h", "1h") if timeframe_reports.get(tf, {}).get("trend") not in {"bullish", "bearish"}]
        confidence = 0.3
    else:
        status = "ok" if len(usable) >= 3 else "limited"
        missing = [tf for tf in ("1d", "4h", "1h") if counts.get(tf, 0) == 0]
        confidence = 0.7 if status == "ok" else 0.45

    findings: list[dict[str, Any]] = []
    for tf, report in timeframe_reports.items():
        findings.append({
            "type": "timeframe_snapshot", "timeframe": tf,
            "status": report.get("status"), "trend": report.get("trend"),
            "trend_score": report.get("trend_score"),
            "latest_close": report.get("latest_close"),
            "ema20": report.get("ema20"), "ema50": report.get("ema50"),
            "ema100": report.get("ema100"), "ema200": report.get("ema200"),
            "rsi14": report.get("rsi14"), "atr14": report.get("atr14"),
            "session_vwap": report.get("session_vwap"),
            "support_20": report.get("support_20"),
            "resistance_20": report.get("resistance_20"),
            "market_structure": report.get("market_structure"),
        })

    candidate = analysis.get("candidate_setup", {})
    risks = []
    if missing:
        risks.append({"type": "missing_timeframes", "timeframes": missing})
    if candidate.get("status") == "candidate_for_review":
        risks.append({"type": "setup_requires_confirmation", "reason": candidate.get("reason")})
    if analysis.get("multi_timeframe_bias", "").startswith("mixed"):
        risks.append({"type": "timeframe_disagreement", "bias": analysis.get("multi_timeframe_bias")})

    report: dict[str, Any] = {
        "status": status,
        "summary": (f"Technical snapshot for {symbol.upper()} as of {cutoff.isoformat()}: "
                    f"{analysis.get('multi_timeframe_bias', 'insufficient_data')}."),
        "findings": findings,
        "risks": risks,
        "evidence_refs": [],
        "confidence": confidence,
        "missing_data": missing,
        "data_quality": {
            "candle_counts": counts,
            "excluded_future_candles": excluded_future,
            "excluded_incomplete_candles": excluded_incomplete,
            "excluded_invalid_candles": excluded_invalid,
            "as_of": cutoff.isoformat(),
            "source": "deterministic_india_technical_module",
        },
    }
    if interpreter is not None and any(counts.values()):
        interpreted = interpreter({"symbol": symbol.upper(), "as_of": cutoff.isoformat(),
                                  "technical_data": analysis, "missing_data": missing,
                                  "risks": risks})
        if not isinstance(interpreted, dict):
            raise ValueError("technical interpreter must return a dictionary")
        if isinstance(interpreted.get("summary"), str) and interpreted["summary"].strip():
            report["summary"] = interpreted["summary"].strip()
        if isinstance(interpreted.get("risks"), list):
            report["interpretation_risks"] = interpreted["risks"]
        if isinstance(interpreted.get("confidence"), (int, float)) and not isinstance(interpreted.get("confidence"), bool):
            report["interpretation_confidence"] = max(0.0, min(1.0, float(interpreted["confidence"])))

    trace = list(state.get("workflow_trace") or [])
    trace.append("technical_analyst")
    return {"technical_data": analysis, "technical_analysis": report, "workflow_trace": trace}

"""Validation helpers for graph requests and node outputs."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from tradingagents.india.langgraph_pipeline.state import AnalysisRequest, FinalVerdict

INDIA_TZ = ZoneInfo("Asia/Kolkata")
ALLOWED_TIMEFRAMES = {"1d", "4h", "1h", "15m"}
ALLOWED_VERDICTS = {"BUY", "SELL", "NO_TRADE", "NEEDS_REVIEW"}


def validate_request(request: dict[str, Any]) -> AnalysisRequest:
    """Validate request shape and enforce a timezone-aware point-in-time cutoff."""
    symbol = request.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip():
        raise ValueError("symbol must be a non-empty string without surrounding whitespace")

    exchange = request.get("exchange", "NSE")
    if not isinstance(exchange, str) or not exchange.strip():
        raise ValueError("exchange must be a non-empty string")

    as_of = request.get("as_of")
    if not isinstance(as_of, str) or not as_of.strip():
        raise ValueError("as_of must be an ISO-8601 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of must include a timezone offset")

    timeframes = request.get("timeframes", ["1d", "4h", "1h", "15m"])
    if not isinstance(timeframes, list) or not timeframes:
        raise ValueError("timeframes must be a non-empty list")
    invalid = sorted(set(timeframes) - ALLOWED_TIMEFRAMES)
    if invalid:
        raise ValueError(f"unsupported timeframes: {', '.join(invalid)}")
    if len(set(timeframes)) != len(timeframes):
        raise ValueError("timeframes must not contain duplicates")

    mode = request.get("analysis_mode", "historical")
    if mode not in {"historical", "latest"}:
        raise ValueError("analysis_mode must be 'historical' or 'latest'")

    return {
        "symbol": symbol.upper(),
        "exchange": exchange.upper(),
        "as_of": parsed.isoformat(),
        "timeframes": timeframes,
        "analysis_mode": mode,
    }


def validate_final_verdict(verdict: dict[str, Any]) -> FinalVerdict:
    """Reject malformed decisions and ensure the graph never claims order execution."""
    if not isinstance(verdict, dict):
        raise ValueError("final_verdict must be a dictionary")
    action = verdict.get("verdict")
    if action not in ALLOWED_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(ALLOWED_VERDICTS)}")
    confidence = verdict.get("confidence", 0.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be a number between 0 and 1")
    if verdict.get("live_order_placed", False) is not False:
        raise ValueError("analytical pipeline must never claim or perform live order execution")
    quality = verdict.get("data_quality", "insufficient")
    if quality not in {"good", "limited", "insufficient"}:
        raise ValueError("data_quality must be good, limited, or insufficient")
    return {
        **verdict,
        "verdict": action,
        "confidence": float(confidence),
        "data_quality": quality,
        "live_order_placed": False,
    }

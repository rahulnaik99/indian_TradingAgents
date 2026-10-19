"""Evidence collection for the new LangGraph pipeline.

Provider access is injectable for deterministic tests. Default adapters are loaded
lazily, so importing this module never performs network calls or requires API keys.
Only evidence known at or before the request's ``as_of`` cutoff is returned.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from tradingagents.india.langgraph_pipeline.contracts import validate_request

INDIA_TZ = ZoneInfo("Asia/Kolkata")
_TF_TO_DAYS = {"1d": 450, "4h": 95, "1h": 95, "15m": 95}


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _jsonable_candle(candle: Any) -> dict[str, Any]:
    return {
        "symbol": candle.symbol,
        "timeframe": candle.timeframe.value,
        "open_time": candle.open_time.isoformat(),
        "close_time": candle.close_time.isoformat(),
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "source_provider": candle.source_provider,
        "retrieved_at": candle.retrieved_at.isoformat(),
        "is_complete": candle.is_complete,
        "quality_flags": list(candle.quality_flags),
    }


def _default_market_client() -> Any:
    from tradingagents.india.providers.fyers import FyersMarketDataClient
    return FyersMarketDataClient.from_env()


def _default_store() -> Any:
    from tradingagents.india.storage import IndiaStore
    return IndiaStore()


def _default_research_service() -> Any:
    from tradingagents.india.events import WebResearchService
    return WebResearchService()


class EvidenceCollector:
    """Collect historical candles and locally stored/researched evidence.

    ``market_client``, ``store``, and ``research_service`` can be injected in tests
    or deployments. Research is opt-in per call; collection does not trigger broad,
    potentially expensive web research automatically.
    """

    def __init__(self, *, market_client: Any | None = None, store: Any | None = None,
                 research_service: Any | None = None, clock: Callable[[], datetime] | None = None):
        self.market_client = market_client
        self.store = store
        self.research_service = research_service
        self.clock = clock or (lambda: datetime.now(INDIA_TZ))

    def collect(self, request_input: dict[str, Any], *, search_missing: bool = False) -> dict[str, Any]:
        request = validate_request(request_input)
        cutoff = datetime.fromisoformat(request["as_of"])
        cutoff_local = cutoff.astimezone(INDIA_TZ)
        symbol = request["symbol"]
        errors: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        candles_by_tf: dict[str, list[dict[str, Any]]] = {}
        market_status = "unavailable"

        client = self.market_client
        if client is None:
            try:
                client = _default_market_client()
            except Exception as exc:
                errors.append({"component": "market_data", "type": type(exc).__name__, "message": str(exc)})

        if client is not None:
            for timeframe in request["timeframes"]:
                start = (cutoff_local.date() - timedelta(days=_TF_TO_DAYS[timeframe]))
                try:
                    candles = client.get_history(symbol, start, cutoff_local.date(), timeframe)
                    # Filter by candle close time, not just open time: a candle still
                    # forming at the requested cutoff is never treated as complete.
                    rows = [_jsonable_candle(c) for c in candles
                            if c.close_time <= cutoff and c.is_complete]
                    candles_by_tf[timeframe] = rows
                    calls.append({"tool": "fyers.get_history", "status": "ok", "timeframe": timeframe,
                                  "count_returned": len(candles), "count_usable": len(rows),
                                  "cutoff": cutoff.isoformat()})
                except Exception as exc:
                    candles_by_tf[timeframe] = []
                    errors.append({"component": "market_data", "timeframe": timeframe,
                                   "type": type(exc).__name__, "message": str(exc)})
                    calls.append({"tool": "fyers.get_history", "status": "error", "timeframe": timeframe})
            market_status = "ok" if any(candles_by_tf.values()) else "unavailable"

        store = self.store
        if store is None:
            try:
                store = _default_store()
            except Exception as exc:
                errors.append({"component": "local_store", "type": type(exc).__name__, "message": str(exc)})

        facts: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        calendar_events: list[dict[str, Any]] = []
        if store is not None:
            try:
                facts = [x for x in store.list_facts(symbol, limit=2000)
                         if _parse_ts(x.get("published_at")) is not None
                         and _parse_ts(x.get("published_at")) <= cutoff]
                calls.append({"tool": "store.list_facts", "status": "ok", "count_usable": len(facts)})
            except Exception as exc:
                errors.append({"component": "fundamental_data", "type": type(exc).__name__, "message": str(exc)})
            try:
                events = [x for x in store.list_events(symbol, limit=500)
                          if _parse_ts(x.get("published_at")) is not None
                          and _parse_ts(x.get("published_at")) <= cutoff]
                calls.append({"tool": "store.list_events", "status": "ok", "count_usable": len(events)})
            except Exception as exc:
                errors.append({"component": "events", "type": type(exc).__name__, "message": str(exc)})
            try:
                start_day = (cutoff_local.date() - timedelta(days=120)).isoformat()
                end_day = (cutoff_local.date() + timedelta(days=180)).isoformat()
                raw_calendar = store.list_calendar_events(start_day, end_day)
                # Calendar event date is not evidence of when it became known.
                # Require a known-at/created timestamp no later than cutoff.
                calendar_events = [x for x in raw_calendar
                                   if (_parse_ts(x.get("known_at") or x.get("created_at")) is not None
                                       and _parse_ts(x.get("known_at") or x.get("created_at")) <= cutoff)]
                calls.append({"tool": "store.list_calendar_events", "status": "ok",
                              "count_usable": len(calendar_events)})
            except Exception as exc:
                errors.append({"component": "calendar_events", "type": type(exc).__name__, "message": str(exc)})

        news: list[dict[str, Any]] = []
        if search_missing:
            research = self.research_service
            if research is None:
                try:
                    research = _default_research_service()
                except Exception as exc:
                    errors.append({"component": "news_research", "type": type(exc).__name__, "message": str(exc)})
            if research is not None:
                try:
                    results = research.search(f'"{symbol}" stock results financial news', max_results=12)
                    for item in results:
                        published = _parse_ts(item.get("published_at") or item.get("published") or item.get("date"))
                        # Unknown publication time is excluded for historical analyses.
                        if published is None or published > cutoff:
                            continue
                        news.append({
                            "source": item.get("source") or "web_search",
                            "source_url": item.get("url") or item.get("link") or "",
                            "published_at": published.isoformat(),
                            "retrieved_at": self.clock().astimezone(INDIA_TZ).isoformat(),
                            "content": item.get("title") or item.get("snippet") or item.get("summary") or "",
                        })
                    calls.append({"tool": "web_research.search", "status": "ok", "count_usable": len(news)})
                except Exception as exc:
                    errors.append({"component": "news_research", "type": type(exc).__name__, "message": str(exc)})
                    calls.append({"tool": "web_research.search", "status": "error"})

        fundamental_data = {
            "status": "available" if facts else "insufficient_data",
            "facts": facts,
            "fact_count": len(facts),
        }
        events_data = events + calendar_events
        return {
            "request": request,
            "market_data": {"status": market_status, "candles_by_timeframe": candles_by_tf},
            "fundamental_data": fundamental_data,
            "news_data": news,
            "events_data": events_data,
            "evidence_metadata": {
                "as_of": cutoff.isoformat(),
                "retrieved_at": self.clock().astimezone(INDIA_TZ).isoformat(),
                "market_candle_counts": {tf: len(rows) for tf, rows in candles_by_tf.items()},
                "financial_fact_count": len(facts),
                "event_count": len(events_data),
                "news_count": len(news),
                "point_in_time_policy": "Only completed candles and timestamped evidence known by as_of are included; undated news/facts/events are excluded.",
            },
            "tool_calls": calls,
            "errors": errors,
            "status": "completed" if not errors else ("partial" if any(candles_by_tf.values()) or facts or events_data or news else "failed"),
            "workflow_trace": ["collect_evidence"],
        }


def collect_evidence(state: dict[str, Any], *, collector: EvidenceCollector | None = None,
                     search_missing: bool = False) -> dict[str, Any]:
    """LangGraph-compatible node wrapper. Returns only state updates."""
    if not isinstance(state, dict) or not isinstance(state.get("request"), dict):
        raise ValueError("state.request is required")
    result = (collector or EvidenceCollector()).collect(state["request"], search_missing=search_missing)
    return result

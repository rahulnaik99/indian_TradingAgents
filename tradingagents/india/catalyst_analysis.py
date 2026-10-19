"""Deterministic corporate-event and catalyst triage for India equity research.

This module interprets already-retrieved evidence; it does not fetch news or
calendar data and does not infer event direction from the event category.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any


CORPORATE_CATEGORIES = {
    "quarterly_result", "quarterly_results", "dividend", "buyback",
    "bonus_split", "board_meeting", "block_deal", "ipo", "index_change",
    "corporate_action", "regulatory_action",
}


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def analyze_catalysts(*, calendar_events: list[dict[str, Any]] | None,
                      news_events: list[dict[str, Any]] | None, as_of: str,
                      symbol: str | None = None, upcoming_days: int = 30,
                      recent_days: int = 7) -> dict[str, Any]:
    """Summarize eligible corporate catalysts without fabricating direction.

    Calendar entries must have a known-at timestamp (known_at, published_at, or
    created_at) no later than the analysis cutoff. Entries without that timestamp
    are excluded from historical eligibility and counted as unknown provenance.
    News entries must have a publication timestamp no later than the cutoff.
    """
    cutoff = _parse_dt(as_of)
    if cutoff is None:
        raise ValueError("as_of must be a timezone-aware ISO timestamp")
    cutoff_day = cutoff.date()
    symbol = (symbol or "").upper()
    upcoming: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    excluded_future = 0
    excluded_unknown_time = 0
    excluded_other_symbol = 0

    for row in calendar_events or []:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").upper()
        if symbol and row_symbol and row_symbol != symbol:
            excluded_other_symbol += 1
            continue
        known_at = next((row.get(k) for k in ("known_at", "published_at", "created_at") if row.get(k)), None)
        known_dt = _parse_dt(known_at)
        if known_dt is None:
            excluded_unknown_time += 1
            continue
        if known_dt > cutoff:
            excluded_future += 1
            continue
        raw_day = row.get("event_date") or row.get("date")
        try:
            event_day = date.fromisoformat(str(raw_day)[:10])
        except (TypeError, ValueError):
            continue
        days = (event_day - cutoff_day).days
        category = str(row.get("category") or "other").casefold()
        is_corporate = category in CORPORATE_CATEGORIES or any(
            term in " ".join(str(row.get(k) or "") for k in ("title", "description", "tags")).casefold()
            for term in ("earnings", "results", "dividend", "buyback", "bonus", "split", "board meeting", "merger", "acquisition", "fund rais")
        )
        item = {"title": row.get("title"), "category": category, "event_date": event_day.isoformat(),
                "days_from_as_of": days, "verified": bool(row.get("verified")),
                "importance": row.get("importance", "unknown"), "source_name": row.get("source_name"),
                "source_url": row.get("source_url"), "symbol": row_symbol or None,
                "corporate_catalyst": is_corporate,
                "direction": "unknown",
                "note": "Event category alone does not establish price direction."}
        if 0 <= days <= max(0, upcoming_days):
            upcoming.append(item)
        elif -max(0, recent_days) <= days < 0:
            recent.append(item)

    eligible_news = []
    for row in news_events or []:
        if not isinstance(row, dict):
            continue
        published = _parse_dt(row.get("published_at"))
        if published is None or published > cutoff:
            continue
        eligible_news.append({"title": row.get("title"), "event_type": row.get("event_type"),
                              "direction": row.get("direction", "uncertain"),
                              "published_at": published.isoformat(), "source_url": row.get("source_url"),
                              "severity": row.get("severity", "low")})

    upcoming.sort(key=lambda x: (x["days_from_as_of"], not x["verified"]))
    recent.sort(key=lambda x: x["days_from_as_of"], reverse=True)
    material = [e for e in upcoming if e["importance"] in {"high", "critical"} or e["category"] in CORPORATE_CATEGORIES]
    return {"status": "available" if upcoming or recent or eligible_news else "no_eligible_catalysts",
            "as_of": cutoff.isoformat(), "symbol": symbol or None,
            "upcoming_catalysts": upcoming[:50], "recent_catalysts": recent[:30],
            "eligible_news_events": eligible_news[:30], "material_upcoming_count": len(material),
            "excluded_future_known_at": excluded_future,
            "excluded_unknown_known_at": excluded_unknown_time,
            "excluded_other_symbol": excluded_other_symbol,
            "warnings": (["Calendar events without a known-at timestamp were excluded; historical catalyst completeness may be limited."] if excluded_unknown_time else []) +
                        ["Calendar and news classifications are evidence triage, not a prediction of price direction."]}

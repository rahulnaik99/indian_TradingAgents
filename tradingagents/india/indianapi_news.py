"""IndianAPI news feed (https://stock.indianapi.in/news), authenticated with INDIANAPI_API_KEY from .env.

Items are normalised to the same shape as the project's other news events so they flow
through ``classify_event`` / ``score_news_item`` and the saved-events UI unchanged.
The key is read only from the environment, sent only as the ``x-api-key`` header, and is
scrubbed from any error text. Responses are cached briefly to protect your API quota.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from tradingagents.india.event_calendar import CalendarSourceError, scrub
from tradingagents.india.events import classify_event

IST = ZoneInfo("Asia/Kolkata")
_CACHE: dict[str, Any] = {"at": 0.0, "items": None, "key_hash": None}
_LOCK = threading.Lock()


def _published_with_tz(value: Any) -> str | None:
    """IndianAPI sends naive timestamps in IST; attach the zone so they are not read as UTC
    (which would make fresh items look future-dated and get filtered out)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc).isoformat()


class IndianApiNewsClient:
    def __init__(self, session: Any | None = None, timeout: float = 15.0):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.base = os.getenv("INDIANAPI_BASE_URL", "https://stock.indianapi.in").rstrip("/")
        self.key = os.getenv("INDIANAPI_API_KEY", "").strip()
        try:
            self.cache_seconds = max(0, int(os.getenv("INDIANAPI_CACHE_SECONDS", "600")))
        except ValueError:
            self.cache_seconds = 600

    def configured(self) -> bool:
        return bool(self.key)

    def fetch(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not self.key:
            raise CalendarSourceError("INDIANAPI_API_KEY is not set in .env.")
        key_hash = hashlib.sha256(self.key.encode()).hexdigest()[:12]
        with _LOCK:
            fresh = time.monotonic() - _CACHE["at"] < self.cache_seconds
            if not force and fresh and _CACHE["items"] is not None and _CACHE["key_hash"] == key_hash:
                return list(_CACHE["items"])
        try:
            response = self.session.get(f"{self.base}/news", headers={"x-api-key": self.key, "Accept": "application/json"},
                                        timeout=self.timeout)
        except requests.RequestException as exc:
            raise CalendarSourceError(scrub(f"IndianAPI request failed ({type(exc).__name__}).")) from exc
        if response.status_code in (401, 403):
            raise CalendarSourceError(f"IndianAPI rejected the API key (HTTP {response.status_code}). Check INDIANAPI_API_KEY.")
        if response.status_code == 429:
            raise CalendarSourceError("IndianAPI rate limit reached (HTTP 429). Cached data is reused for "
                                      f"{self.cache_seconds}s; wait before refreshing again.")
        if not response.ok:
            raise CalendarSourceError(f"IndianAPI returned HTTP {response.status_code}.")
        try:
            rows = response.json()
        except ValueError as exc:
            raise CalendarSourceError("IndianAPI returned a non-JSON response.") from exc
        if isinstance(rows, dict):
            rows = rows.get("news") or rows.get("data") or rows.get("results") or []
        items = self.normalize(rows if isinstance(rows, list) else [])
        with _LOCK:
            _CACHE.update(at=time.monotonic(), items=items, key_hash=key_hash)
        return list(items)

    @staticmethod
    def normalize(rows: list[Any]) -> list[dict[str, Any]]:
        retrieved = datetime.now(timezone.utc).isoformat()
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title, url = str(row.get("title") or "").strip(), str(row.get("url") or "").strip()
            if not title or not url.startswith(("http://", "https://")):
                continue
            summary = str(row.get("summary") or "").strip()
            out.append({
                "event_id": hashlib.sha256(f"{url}|{title}".encode()).hexdigest()[:24],
                "title": title, "summary": summary, "source_name": str(row.get("source") or "IndianAPI"),
                "source_url": url, "image_url": row.get("image_url"), "topics": row.get("topics") or [],
                "published_at": _published_with_tz(row.get("pub_date")), "retrieved_at": retrieved,
                **classify_event(title, summary, url), "provider": "indianapi",
                "verification_status": "unverified_secondary_lead",
            })
        return out


def clear_cache() -> None:
    with _LOCK:
        _CACHE.update(at=0.0, items=None, key_hash=None)

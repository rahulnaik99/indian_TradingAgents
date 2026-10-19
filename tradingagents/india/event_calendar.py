"""Market event calendar: Fed/RBI, NSE corporate events, crypto events, news-detected events.

Every event carries its source and a ``verified`` flag. Official/exchange data is
verified; events extracted from news text are *leads* (``status="detected_from_news"``)
and are shown as such. Providers fail soft: one blocked source never breaks the others.

Run a refresh from cron with:  python -m tradingagents.india.event_calendar refresh
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(__file__).parent / "data"

# Colour/priority table. The UI colours a day box by the highest-priority category on that day.
CATEGORIES: dict[str, dict[str, Any]] = {
    "fed_meeting":      {"label": "Fed / FOMC",        "color": "#d4a300", "tint": "#fff3b0", "priority": 1},
    "rbi_policy":       {"label": "RBI policy",        "color": "#b7791f", "tint": "#fde8b5", "priority": 2},
    "quarterly_result": {"label": "Quarterly result",  "color": "#2563eb", "tint": "#dbeafe", "priority": 3},
    "dividend":         {"label": "Dividend",          "color": "#15803d", "tint": "#dcfce7", "priority": 4},
    "buyback":          {"label": "Buyback",           "color": "#7e22ce", "tint": "#f3e8ff", "priority": 5},
    "bonus_split":      {"label": "Bonus / split",     "color": "#0e7490", "tint": "#cffafe", "priority": 6},
    "block_deal":       {"label": "Block deal",        "color": "#c2410c", "tint": "#ffedd5", "priority": 7},
    "board_meeting":    {"label": "Board meeting",     "color": "#0f766e", "tint": "#ccfbf1", "priority": 8},
    "ipo":              {"label": "IPO",               "color": "#4338ca", "tint": "#e0e7ff", "priority": 9},
    "crypto_event":     {"label": "Crypto event",      "color": "#be185d", "tint": "#fce7f3", "priority": 10},
    "crypto_unlock":    {"label": "Token unlock",      "color": "#9d174d", "tint": "#fbcfe8", "priority": 11},
    "market_holiday":   {"label": "NSE holiday",       "color": "#64748b", "tint": "#e2e8f0", "priority": 12},
    "other":            {"label": "Other",             "color": "#475569", "tint": "#f1f5f9", "priority": 13},
}
ASSET_SCOPES = ("NSE", "CRYPTO")
DEFAULT_SOURCES = ("static", "nse", "coinmarketcal", "indianapi_news", "crypto_news")
SECRET_ENV_VARS = ("INDIANAPI_API_KEY", "COINMARKETCAL_API_KEY", "NSE_COOKIES", "GOOGLE_CLIENT_SECRET")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> date:
    return datetime.now(IST).date()


def _iso(value: date) -> str:
    return value.isoformat()


def scrub(text: str) -> str:
    """Remove any configured secret from an error message before it leaves the server."""
    for name in SECRET_ENV_VARS:
        secret = os.getenv(name, "").strip()
        if len(secret) >= 6:
            text = text.replace(secret, "***")
    return text


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def parse_date_any(value: Any) -> date | None:
    """Parse the date formats seen from NSE/APIs: 12-Oct-2026, 12-10-2026, 2026-10-12[T..], Oct 12, 2026."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    first_token = raw.split()[0]
    for candidate in (raw, first_token, raw[:10]):
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def make_event(*, day: date, category: str, title: str, assets: list[str], provider: str, source_name: str,
               end_day: date | None = None, symbol: str | None = None, description: str = "",
               source_url: str = "", verified: bool = False, status: str = "scheduled",
               importance: str = "medium", tags: list[str] | None = None) -> dict[str, Any]:
    if category not in CATEGORIES:
        category = "other"
    symbol = (symbol or "").strip().upper() or None
    end_iso = _iso(end_day) if end_day and end_day > day else None
    key = f"{category}|{symbol or ''}|{_iso(day)}|{_norm(title)}"
    return {
        "event_id": hashlib.sha256(key.encode()).hexdigest()[:24], "date": _iso(day), "end_date": end_iso,
        "category": category, "tags": sorted(set(tags or [category])), "symbol": symbol,
        "title": title.strip()[:240], "description": description.strip()[:1200],
        "assets": [a for a in assets if a in ASSET_SCOPES] or ["NSE"],
        "source_name": source_name, "source_url": source_url if source_url.startswith(("http://", "https://")) else "",
        "provider": provider, "verified": bool(verified), "status": status, "importance": importance,
        "created_at": _now(),
    }


# ---------------------------------------------------------------- classification
_PURPOSE_RULES: tuple[tuple[str, str], ...] = (
    ("buyback", r"buy[\s-]?back"),
    ("quarterly_result", r"financial results?|quarterly results?|\bresults?\b|earnings|audited|unaudited"),
    ("dividend", r"dividend"),
    ("bonus_split", r"\bbonus\b|stock split|sub-?division|split of|\brights?\b"),
    ("ipo", r"\bipo\b|initial public offer"),
    ("board_meeting", r"board meeting|fund[\s-]?rais|preferential|merger|amalgamation|demerger|acquisition|\besop\b|stock option|allotment|\bqip\b"),
)


def classify_purpose(text: str) -> tuple[str, list[str]]:
    """Return (primary_category, all matching tags) for an NSE purpose/subject string."""
    low = (text or "").casefold()
    tags = [cat for cat, pattern in _PURPOSE_RULES if re.search(pattern, low)]
    return (tags[0] if tags else "board_meeting"), (tags or ["board_meeting"])


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row[key]
    return None


# ---------------------------------------------------------------- static seeds
def static_events(path: Path | None = None, holidays_path: Path | None = None) -> list[dict[str, Any]]:
    """Fed (FOMC), RBI MPC and NSE trading holidays from the repo's checked-in CSVs."""
    events: list[dict[str, Any]] = []
    macro = path or DATA_DIR / "macro_events.csv"
    with macro.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            start, end = parse_date_any(row["date"]), parse_date_any(row.get("end_date"))
            if start is None:
                continue
            tentative = (row.get("status") or "").strip() == "tentative"
            events.append(make_event(
                day=start, end_day=end, category=row["category"].strip(), title=row["title"],
                assets=[a for a in row["assets"].split("|") if a], description=row.get("description", ""),
                provider="static", source_name=row["source_name"], source_url=row.get("source_url", ""),
                verified=not tentative, status="tentative" if tentative else "scheduled", importance="high"))
    holidays = holidays_path or DATA_DIR / "nse_equity_holidays_2026.csv"
    if holidays.is_file():
        with holidays.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                day = parse_date_any(row.get("date"))
                if day is None:
                    continue
                special = str(row.get("special_session", "")).strip().lower() == "true"
                title = f"NSE special session: {row['name']}" if special else f"NSE closed: {row['name']}"
                events.append(make_event(
                    day=day, category="market_holiday", title=title, assets=["NSE"], provider="static",
                    source_name="NSE holiday list (repo CSV)", verified=True, importance="medium",
                    description="Special session timings are announced by NSE circular." if special else "Equity segment closed."))
    return events


# ---------------------------------------------------------------- NSE provider
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")


class CalendarSourceError(RuntimeError):
    pass


class NseCalendarClient:
    """Unofficial NSE JSON endpoints. NSE rejects clients without browser cookies.

    Cookies come from (1) NSE_COOKIES, a Cookie header copied from your browser's dev tools,
    or (2) an automatic warm-up request to the NSE home page. NSE may still block automated
    or datacentre traffic; the error then says so and other sources keep working.
    """
    BASE = "https://www.nseindia.com"

    def __init__(self, session: Any | None = None, timeout: float = 20.0):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.cookie = os.getenv("NSE_COOKIES", "").strip()
        self.headers = {
            "User-Agent": os.getenv("NSE_USER_AGENT", "").strip() or DEFAULT_UA,
            "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{self.BASE}/companies-listing/corporate-filings-event-calendar",
        }
        if self.cookie:
            self.headers["Cookie"] = self.cookie
        self._warmed = bool(self.cookie)

    def configured(self) -> bool:
        return True  # keyless; NSE_COOKIES is an optional fallback

    def _warm(self) -> None:
        if self._warmed:
            return
        try:
            self.session.get(self.BASE, headers={**self.headers, "Accept": "text/html"}, timeout=self.timeout)
        except requests.RequestException:
            pass  # the API call below reports the real failure
        self._warmed = True

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        self._warm()
        try:
            response = self.session.get(f"{self.BASE}{path}", params=params, headers=self.headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise CalendarSourceError(scrub(f"NSE request failed ({type(exc).__name__}).")) from exc
        if response.status_code in (401, 403):
            raise CalendarSourceError("NSE blocked the request (HTTP %s). Copy a fresh Cookie header from your browser "
                                      "into NSE_COOKIES in .env and retry." % response.status_code)
        if not response.ok:
            raise CalendarSourceError(f"NSE returned HTTP {response.status_code} for {path}.")
        try:
            return response.json()
        except ValueError as exc:
            raise CalendarSourceError("NSE returned a non-JSON page (likely a bot-check). Set NSE_COOKIES in .env.") from exc

    @staticmethod
    def _window(start: date, end: date) -> dict[str, str]:
        return {"index": "equities", "from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")}

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            payload = payload.get("data") or payload.get("rows") or []
        return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []

    def event_calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        events = []
        for row in self._rows(self._get("/api/event-calendar", self._window(start, end))):
            day = parse_date_any(_first(row, "date", "bm_date"))
            symbol = _first(row, "symbol", "bm_symbol")
            if day is None or not symbol:
                continue
            purpose = str(_first(row, "purpose", "bm_purpose") or "Board meeting")
            detail = str(_first(row, "bm_desc", "description") or "")
            company = str(_first(row, "company", "sm_name") or symbol)
            category, tags = classify_purpose(f"{purpose} {detail}")
            events.append(make_event(
                day=day, category=category, tags=tags, symbol=str(symbol), assets=["NSE"], provider="nse_event_calendar",
                title=f"{symbol}: {purpose}", description=f"{company}. {detail}".strip(),
                source_name="NSE event calendar", source_url=f"{self.BASE}/companies-listing/corporate-filings-event-calendar",
                verified=True, importance="high" if category in ("quarterly_result", "buyback") else "medium"))
        return events

    def board_meetings(self, start: date, end: date) -> list[dict[str, Any]]:
        events = []
        for row in self._rows(self._get("/api/corporate-board-meetings", self._window(start, end))):
            day = parse_date_any(_first(row, "bm_date", "date"))
            symbol = _first(row, "bm_symbol", "symbol")
            if day is None or not symbol:
                continue
            purpose = str(_first(row, "bm_purpose", "purpose") or "Board meeting")
            detail = str(_first(row, "bm_desc", "description") or "")
            company = str(_first(row, "sm_name", "company") or symbol)
            category, tags = classify_purpose(f"{purpose} {detail}")
            events.append(make_event(
                day=day, category=category, tags=tags, symbol=str(symbol), assets=["NSE"], provider="nse_board_meetings",
                title=f"{symbol}: board meeting - {purpose}", description=f"{company}. {detail}".strip(),
                source_name="NSE board meetings", source_url=f"{self.BASE}/companies-listing/corporate-filings-board-meetings",
                verified=True, importance="high" if category in ("quarterly_result", "buyback") else "medium"))
        return events

    def corporate_actions(self, start: date, end: date) -> list[dict[str, Any]]:
        events = []
        for row in self._rows(self._get("/api/corporates-corporateActions", self._window(start, end))):
            symbol = _first(row, "symbol")
            subject = str(_first(row, "subject", "purpose") or "Corporate action")
            ex_date = parse_date_any(_first(row, "exDate", "ex_date"))
            record = parse_date_any(_first(row, "recDate", "record_date"))
            day = ex_date or record
            if day is None or not symbol:
                continue
            category, tags = classify_purpose(subject)
            if category in ("board_meeting", "quarterly_result"):
                category, tags = "other", ["other"]
            note = f"{_first(row, 'comp', 'company') or symbol}. Ex-date: {ex_date or 'n/a'}; record date: {record or 'n/a'}."
            events.append(make_event(
                day=day, category=category, tags=tags, symbol=str(symbol), assets=["NSE"], provider="nse_corporate_actions",
                title=f"{symbol}: {subject}" + (" (ex-date)" if ex_date else " (record date)"), description=note,
                source_name="NSE corporate actions", source_url=f"{self.BASE}/companies-listing/corporate-filings-actions",
                verified=True, importance="medium"))
        return events

    def block_deals(self) -> list[dict[str, Any]]:
        """NSE publishes block deals for the current session only, so these are same-day events."""
        payload = self._get("/api/block-deal")
        rows = self._rows(payload)
        stamp = parse_date_any(payload.get("timestamp")) if isinstance(payload, dict) else None
        day = stamp or _today()
        per_symbol: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = str(_first(row, "symbol") or "").upper()
            if not symbol:
                continue
            agg = per_symbol.setdefault(symbol, {"trades": 0, "qty": 0.0})
            agg["trades"] += 1
            try:
                agg["qty"] += float(_first(row, "qty", "quantity") or 0)
            except (TypeError, ValueError):
                pass
        return [make_event(
            day=day, category="block_deal", symbol=symbol, assets=["NSE"], provider="nse_block_deals",
            title=f"{symbol}: block deal ({agg['trades']} trade{'s' if agg['trades'] != 1 else ''})",
            description=f"Total quantity {agg['qty']:,.0f} shares across {agg['trades']} reported block trade(s).",
            source_name="NSE block deals", source_url=f"{self.BASE}/market-data/block-deal",
            verified=True, status="occurred", importance="medium") for symbol, agg in per_symbol.items()]

    def fetch_all(self, start: date, end: date) -> list[dict[str, Any]]:
        """Fetch all NSE feeds; succeed if at least one works, otherwise raise the first error."""
        events: list[dict[str, Any]] = []
        errors: list[str] = []
        for label, call in (("event calendar", lambda: self.event_calendar(start, end)),
                            ("board meetings", lambda: self.board_meetings(start, end)),
                            ("corporate actions", lambda: self.corporate_actions(start, end)),
                            ("block deals", self.block_deals)):
            try:
                events.extend(call())
            except CalendarSourceError as exc:
                errors.append(f"{label}: {exc}")
        if not events and errors:
            raise CalendarSourceError(" | ".join(errors[:2]))
        return events


# ---------------------------------------------------------------- crypto providers
class CoinMarketCalClient:
    """Crypto events from CoinMarketCal (free API key required): COINMARKETCAL_API_KEY."""
    BASE = os.getenv("COINMARKETCAL_BASE_URL", "https://developers.coinmarketcal.com/v1").rstrip("/")

    def __init__(self, session: Any | None = None, timeout: float = 20.0):
        self.session, self.timeout = session or requests.Session(), timeout
        self.key = os.getenv("COINMARKETCAL_API_KEY", "").strip()

    def configured(self) -> bool:
        return bool(self.key)

    def fetch(self, start: date, end: date, coins: str | None = None) -> list[dict[str, Any]]:
        if not self.key:
            raise CalendarSourceError("COINMARKETCAL_API_KEY is not set in .env.")
        params = {"dateRangeStart": start.strftime("%m/%d/%Y"), "dateRangeEnd": end.strftime("%m/%d/%Y"),
                  "coins": coins or os.getenv("CALENDAR_CRYPTO_COINS", "bitcoin,ethereum"), "max": "150", "sortBy": "created_desc"}
        try:
            response = self.session.get(f"{self.BASE}/events", params=params, timeout=self.timeout,
                                        headers={"x-api-key": self.key, "Accept": "application/json"})
        except requests.RequestException as exc:
            raise CalendarSourceError(scrub(f"CoinMarketCal request failed ({type(exc).__name__}).")) from exc
        if response.status_code in (401, 403):
            raise CalendarSourceError("CoinMarketCal rejected the API key (HTTP %s)." % response.status_code)
        if not response.ok:
            raise CalendarSourceError(f"CoinMarketCal returned HTTP {response.status_code}.")
        try:
            body = response.json().get("body") or []
        except (ValueError, AttributeError) as exc:
            raise CalendarSourceError("CoinMarketCal returned an unexpected response.") from exc
        events = []
        for row in body:
            if not isinstance(row, dict):
                continue
            day = parse_date_any(row.get("date_event"))
            title = row.get("title")
            title = (title.get("en") if isinstance(title, dict) else title) or ""
            if day is None or not str(title).strip():
                continue
            coins_list = [c.get("symbol") for c in (row.get("coins") or []) if isinstance(c, dict) and c.get("symbol")]
            cats = [c.get("name") for c in (row.get("categories") or []) if isinstance(c, dict) and c.get("name")]
            is_unlock = any("unlock" in str(c).casefold() or "vesting" in str(c).casefold() for c in cats)
            desc = row.get("description")
            desc = (desc.get("en") if isinstance(desc, dict) else desc) or ""
            pct = row.get("percentage")
            events.append(make_event(
                day=day, category="crypto_unlock" if is_unlock else "crypto_event", assets=["CRYPTO"],
                title=f"{'/'.join(coins_list[:3]) or 'Crypto'}: {title}", provider="coinmarketcal",
                description=f"{desc} Categories: {', '.join(cats) or 'n/a'}." + (f" Community credibility: {pct}%." if pct is not None else ""),
                source_name="CoinMarketCal", source_url=str(row.get("source") or row.get("proof") or ""),
                verified=False, status="community_reported", importance="high" if row.get("is_hot") else "medium"))
        return events


class CryptoNewsClient:
    """Headlines from the free cryptocurrency.cv API (or your self-hosted copy via CRYPTO_NEWS_BASE_URL)."""

    def __init__(self, session: Any | None = None, timeout: float = 20.0):
        self.session, self.timeout = session or requests.Session(), timeout
        self.base = os.getenv("CRYPTO_NEWS_BASE_URL", "https://cryptocurrency.cv").rstrip("/")

    def configured(self) -> bool:
        return True

    def fetch(self, limit: int = 60) -> list[dict[str, Any]]:
        try:
            response = self.session.get(f"{self.base}/api/news", params={"limit": limit}, timeout=self.timeout,
                                        headers={"User-Agent": "TradingAgents-India/0.3", "Accept": "application/json"})
            response.raise_for_status()
            articles = response.json().get("articles") or []
        except (requests.RequestException, ValueError, AttributeError) as exc:
            raise CalendarSourceError(scrub(f"Crypto news request failed ({type(exc).__name__}). "
                                            f"Check CRYPTO_NEWS_BASE_URL ({self.base}).")) from exc
        return [{"title": a.get("title") or "", "summary": a.get("description") or "", "source_url": a.get("link") or "",
                 "source_name": a.get("source") or "cryptocurrency.cv", "published_at": a.get("pubDate")}
                for a in articles if isinstance(a, dict) and a.get("title")]


# ---------------------------------------------------------------- news -> calendar detection
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MON = r"(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sept|sep|october|oct|november|nov|december|dec)\b\.?"
_DATE_RE = re.compile(rf"\b(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}(?:,?\s+(\d{{4}}))?|{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?)\b", re.I)

# (category, regex, assets, importance). Order = priority within one sentence.
_TRIGGERS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("fed_meeting", r"\bfomc\b|federal reserve.{0,40}(meeting|decision)|\bfed (policy )?(meeting|decision)", ("NSE", "CRYPTO"), "high"),
    ("rbi_policy", r"\brbi\b.{0,40}(policy|mpc|monetary)|monetary policy committee|\bmpc meeting", ("NSE",), "high"),
    ("buyback", r"buy[\s-]?back", ("NSE",), "medium"),
    ("quarterly_result", r"\bq[1-4]\b.{0,30}(result|earning)|quarterly results?|financial results?|results? (on|date)|earnings (on|date|call)", ("NSE",), "medium"),
    ("dividend", r"dividend|ex[\s-]?date|record date", ("NSE",), "medium"),
    ("bonus_split", r"bonus (issue|shares)|stock split|sub-?division", ("NSE",), "medium"),
    ("board_meeting", r"board meeting|board (will|to) (meet|consider)", ("NSE",), "medium"),
    ("ipo", r"\bipo\b.{0,80}(open|opens|opening|subscription|clos|listing|lists|allotment)|(opens?|opening|listing).{0,80}\bipo\b", ("NSE",), "medium"),
    ("block_deal", r"block deal", ("NSE",), "low"),
    ("crypto_unlock", r"token unlock|unlocks? .{0,30}tokens?|vesting", ("CRYPTO",), "medium"),
    ("crypto_event", r"halving|hard fork|mainnet|network upgrade|\betf\b.{0,40}(decid|decision|deadline|approval|ruling)|\bsec\b.{0,30}(decid|decision|deadline)|airdrop|snapshot", ("CRYPTO",), "medium"),
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'‘“])|\n+")


def _resolve_date(match: re.Match[str], reference: date) -> date | None:
    day_s, mon_a, year_a, mon_b, day_b, year_b = match.groups()
    token = mon_a or mon_b
    if token == "may":  # lowercase "may" is almost always the verb, not the month
        return None
    month = _MONTHS[token.lower()[:3]]
    day_n = int(day_s or day_b)
    year_s = year_a or year_b
    try:
        if year_s:
            return date(int(year_s), month, day_n)
        candidate = date(reference.year, month, day_n)
        return candidate if candidate >= reference - timedelta(days=60) else date(reference.year + 1, month, day_n)
    except ValueError:
        return None


def detect_events_from_news(items: list[dict[str, Any]], *, scope: str = "NSE", today: date | None = None,
                            provider: str = "news", symbol: str | None = None) -> list[dict[str, Any]]:
    """Extract dated, scheduled-type events from headlines/summaries.

    A sentence must contain BOTH an explicit calendar date and an event trigger. Relative
    phrases ("this week") are ignored on purpose. Results are unverified news leads.
    """
    today = today or _today()
    window_start, window_end = today - timedelta(days=7), today + timedelta(days=400)
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        title = str(item.get("title") or item.get("headline") or "")
        summary = str(item.get("summary") or item.get("description") or "")
        url = str(item.get("source_url") or item.get("url") or "")
        published = item.get("published_at_parsed") or item.get("published_at") or item.get("pub_date")
        reference = parse_date_any(str(published)[:10]) if published else None
        reference = reference or today
        for sentence in _SENTENCE_SPLIT.split(f"{title}. {summary}"):
            sentence = sentence.strip()
            if len(sentence) < 15:
                continue
            for date_match in _DATE_RE.finditer(sentence):
                day = _resolve_date(date_match, reference)
                if day is None or not (window_start <= day <= window_end):
                    continue
                for category, pattern, assets, importance in _TRIGGERS:
                    if scope not in assets or not re.search(pattern, sentence, re.I):
                        continue
                    label = CATEGORIES[category]["label"]
                    event = make_event(
                        day=day, category=category, symbol=item.get("symbol") or symbol, assets=list(assets) if category == "fed_meeting" else [scope],
                        title=f"{label}: {(title or sentence)[:150]}", provider=f"{provider}", status="detected_from_news", importance=importance,
                        description=f"Matched text: \"{sentence[:260]}\" ({item.get('source_name') or item.get('source') or 'unknown source'}). "
                                    "This is a lead extracted from article text, not an exchange filing - verify before relying on the date.",
                        source_name=str(item.get("source_name") or item.get("source") or "News"), source_url=url, verified=False)
                    found.setdefault(event["event_id"], event)
                    break  # one category per date mention
    return list(found.values())


# ---------------------------------------------------------------- read-side helpers
def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the same (category, symbol, date) reported by several sources; prefer verified."""
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        key = (event["category"], event.get("symbol") or "", event["date"], event.get("end_date") or "")
        current = best.get(key)
        rank = (bool(event.get("verified")), bool(event.get("source_url")), len(event.get("description") or ""))
        if current is None or rank > (bool(current.get("verified")), bool(current.get("source_url")), len(current.get("description") or "")):
            best[key] = event
    return sorted(best.values(), key=lambda e: (e["date"], CATEGORIES.get(e["category"], CATEGORIES["other"])["priority"], e["title"]))


def month_bounds(month: str) -> tuple[date, date]:
    first = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def query_events(store: Any, start: date, end: date, *, asset: str = "all", category: str = "all", symbol: str | None = None) -> list[dict[str, Any]]:
    events = dedupe_events(store.list_calendar_events(_iso(start), _iso(end)))
    asset = asset.upper()
    if asset in ASSET_SCOPES:
        events = [e for e in events if asset in e.get("assets", [])]
    if category != "all":
        events = [e for e in events if e["category"] == category or category in e.get("tags", [])]
    if symbol:
        events = [e for e in events if e.get("symbol") == symbol.upper()]
    return events


# ---------------------------------------------------------------- orchestration
def ingest_news_items(store: Any, items: list[dict[str, Any]], *, scope: str = "NSE", provider: str = "news",
                      symbol: str | None = None, today: date | None = None) -> int:
    """Detect scheduled events in news items and save them. Returns the number of NEW calendar rows."""
    return sum(1 for ev in detect_events_from_news(items, scope=scope, provider=provider, symbol=symbol, today=today)
               if store.upsert_calendar_event(ev))


def refresh_calendar(store: Any, sources: list[str] | None = None, *, today: date | None = None, window_days: int = 120,
                     nse: NseCalendarClient | None = None, coinmarketcal: CoinMarketCalClient | None = None,
                     crypto_news: CryptoNewsClient | None = None, indianapi: Any | None = None) -> dict[str, Any]:
    """Refresh the requested sources; every source reports its own status and never raises."""
    today = today or _today()
    start, end = today - timedelta(days=7), today + timedelta(days=window_days)
    wanted = [s for s in (sources or DEFAULT_SOURCES) if s in DEFAULT_SOURCES]
    report: dict[str, Any] = {}

    def save(events: list[dict[str, Any]]) -> tuple[int, int]:
        return len(events), sum(1 for ev in events if store.upsert_calendar_event(ev))

    for name in wanted:
        try:
            if name == "static":
                fetched, new = save(static_events())
            elif name == "nse":
                fetched, new = save((nse or NseCalendarClient()).fetch_all(start, end))
            elif name == "coinmarketcal":
                client = coinmarketcal or CoinMarketCalClient()
                if not client.configured():
                    report[name] = {"status": "skipped", "message": "Set COINMARKETCAL_API_KEY in .env to enable."}
                    continue
                fetched, new = save(client.fetch(start, end))
            elif name == "crypto_news":
                items = (crypto_news or CryptoNewsClient()).fetch()
                events = detect_events_from_news(items, scope="CRYPTO", provider="news:cryptocurrency.cv", today=today)
                fetched, new = save(events)
            elif name == "indianapi_news":
                if indianapi is None:
                    from tradingagents.india.indianapi_news import IndianApiNewsClient
                    indianapi = IndianApiNewsClient()
                if not indianapi.configured():
                    report[name] = {"status": "skipped", "message": "Set INDIANAPI_API_KEY in .env to enable."}
                    continue
                items = indianapi.fetch()
                events = detect_events_from_news(items, scope="NSE", provider="news:indianapi", today=today)
                fetched, new = save(events)
            report[name] = {"status": "ok", "fetched": fetched, "new": new}
            store.set_calendar_meta(f"last_refresh:{name}", _now())
        except Exception as exc:  # noqa: BLE001 - a single provider must never break the refresh
            report[name] = {"status": "error", "message": scrub(f"{type(exc).__name__}: {exc}")[:300]}
    store.set_calendar_meta("last_refresh", _now())
    return report


def source_status(store: Any) -> list[dict[str, Any]]:
    """Which sources are configured and when each last succeeded (never exposes secrets)."""
    rows = [
        ("static", "Fed FOMC + RBI MPC + NSE holidays (checked-in CSV)", True, "Update data/macro_events.csv when new schedules are published."),
        ("nse", "NSE event calendar, board meetings, corporate actions, block deals", True,
         "Keyless; set NSE_COOKIES in .env if NSE blocks the automatic session." + (" NSE_COOKIES is set." if os.getenv("NSE_COOKIES") else "")),
        ("coinmarketcal", "CoinMarketCal crypto events", bool(os.getenv("COINMARKETCAL_API_KEY")), "Needs COINMARKETCAL_API_KEY."),
        ("indianapi_news", "IndianAPI news -> detected dated events", bool(os.getenv("INDIANAPI_API_KEY")), "Needs INDIANAPI_API_KEY."),
        ("crypto_news", "cryptocurrency.cv news -> detected dated events", True,
         f"Base URL: {os.getenv('CRYPTO_NEWS_BASE_URL', 'https://cryptocurrency.cv')}"),
    ]
    return [{"source": key, "description": desc, "configured": ok, "note": note, "last_refresh": store.get_calendar_meta(f"last_refresh:{key}")}
            for key, desc, ok, note in rows]


def main(argv: list[str] | None = None) -> int:
    import argparse
    from dotenv import load_dotenv
    from tradingagents.india.storage import IndiaStore
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    parser = argparse.ArgumentParser(prog="python -m tradingagents.india.event_calendar")
    parser.add_argument("command", choices=["refresh"])
    parser.add_argument("--sources", nargs="*", default=None, help=f"subset of {', '.join(DEFAULT_SOURCES)}")
    parser.add_argument("--db", default=os.getenv("TRADINGAGENTS_DB_PATH", str(Path(__file__).resolve().parents[2] / "data" / "tradingagents_india.sqlite3")))
    args = parser.parse_args(argv)
    print(json.dumps(refresh_calendar(IndiaStore(args.db), args.sources), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

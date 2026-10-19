"""Configurable quality scoring and filtering for Indian equity news.

The lists below are intentionally centralized so source/keyword policy can be
adjusted without changing API or UI code. Scores are heuristics, not truth labels.
"""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

# Tune these lists in one place.
CONFIG: dict[str, Any] = {
    "good_score_min": 60,
    "financial_news_domains": (
        "reuters.com", "bloomberg.com", "cnbctv18.com", "moneycontrol.com",
        "economictimes.indiatimes.com", "livemint.com", "business-standard.com",
        "financialexpress.com", "businessline.com", "mint.com", "ndtvprofit.com",
        "thehindubusinessline.com", "zeebiz.com", "in.investing.com",
    ),
    "official_domains": ("nseindia.com", "bseindia.com", "rbi.org.in", "sebi.gov.in", ".gov.in"),
    "junk_domains": ("wikipedia.org", "youtube.com", "youtu.be", "youtube.music", "facebook.com", "instagram.com", "x.com", "twitter.com", "linkedin.com"),
    "junk_path_terms": ("netbanking", "net-banking", "open-account", "openaccount", "contact-us", "contactus", "customer-care", "careers", "privacy-policy", "terms-and-conditions"),
    "stock_keywords": ("result", "results", "profit", "loss", "npa", "dividend", "rating", "target", "stake", "rbi", "quarter", "earnings", "revenue", "shares", "share price", "stock", "acquisition", "merger", "buyback", "upgrade", "downgrade", "order", "fraud", "penalty", "regulatory", "guidance", "eps", "margin", "deposit", "credit growth"),
    "date_windows_days": (7, 30, 90),
}


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url or "").hostname or "").casefold().removeprefix("www.")


def _domain_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def parse_published(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_english_title(title: str) -> bool:
    letters = [ch for ch in title if ch.isalpha()]
    if not letters:
        return False
    latin = sum("LATIN" in __import__("unicodedata").name(ch, "") for ch in letters)
    return latin / len(letters) >= 0.85


def score_news_item(item: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Return a copy annotated with source_type, relevance_score and quality."""
    row = dict(item)
    title = str(row.get("title") or row.get("headline") or "").strip()
    summary = str(row.get("summary") or row.get("description") or row.get("snippet") or "")
    url = str(row.get("source_url") or row.get("url") or "")
    host = _host(url)
    source_name = str(row.get("source_name") or "").casefold()
    published = parse_published(row.get("published_at") or row.get("published") or row.get("date"))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_days = (now.astimezone(timezone.utc) - published).total_seconds() / 86400 if published else None

    official_name = any(token in source_name for token in ("nse", "bse", "reserve bank of india", "rbi", "sebi", "government of india"))
    financial_name = any(token in source_name for token in ("reuters", "bloomberg", "cnbc", "moneycontrol", "economic times", "livemint", "business standard", "financial express", "businessline", "zee business", "ndtv profit", "investing.com"))
    junk_name = any(token in source_name for token in ("wikipedia", "youtube", "facebook", "instagram", "twitter", "linkedin"))
    if _domain_matches(host, CONFIG["official_domains"]) or official_name:
        source_type, score = "official", 45
    elif _domain_matches(host, CONFIG["financial_news_domains"]) or financial_name:
        source_type, score = "news", 40
    elif _domain_matches(host, CONFIG["junk_domains"]) or junk_name:
        source_type, score = "other", 0
    else:
        source_type, score = "other", 15

    title_l = title.casefold()
    text_l = f"{title} {summary}".casefold()
    keyword_hits = [kw for kw in CONFIG["stock_keywords"] if kw in title_l]
    score += min(25, 10 + 5 * (len(keyword_hits) - 1)) if keyword_hits else 0
    if published:
        if 0 <= age_days <= 30:
            score += 25
        elif 30 < age_days <= 90:
            score += 15
        elif age_days < 0:
            score -= 35
        else:
            score -= 25
    else:
        score -= 20
    if is_english_title(title):
        score += 10
    else:
        score -= 30
    if any(term in urllib.parse.urlparse(url).path.casefold() for term in CONFIG["junk_path_terms"]):
        score -= 45
    if not title or len(title) < 12:
        score -= 15
    if _domain_matches(host, CONFIG["junk_domains"]):
        score = min(score, 10)
    score = max(0, min(100, int(round(score))))
    row.update({"source_type": source_type, "relevance_score": score,
                "quality": "good" if score >= CONFIG["good_score_min"] else "weak",
                "is_english_title": is_english_title(title),
                "published_at_parsed": published.isoformat() if published else None,
                "quality_reasons": {"keyword_hits": keyword_hits, "date_known": bool(published), "age_days": round(age_days, 1) if age_days is not None else None, "source_type": source_type}})
    return row


def filter_news_items(items: list[dict[str, Any]], *, source_type: str = "all", quality: str = "good",
                      days: int = 30, include_undated: bool = False, event_type: str = "all",
                      direction: str = "all", text: str = "", include_hidden: bool = False,
                      now: datetime | None = None) -> list[dict[str, Any]]:
    if days not in CONFIG["date_windows_days"]:
        raise ValueError(f"days must be one of {CONFIG['date_windows_days']}")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now.astimezone(timezone.utc) - timedelta(days=days)
    needle = text.casefold().strip()
    output = []
    for raw in items:
        item = score_news_item(raw, now=now)
        if item.get("hidden") and not include_hidden:
            continue
        if source_type != "all" and item.get("source_type") != source_type:
            continue
        if quality == "good" and item.get("quality") != "good":
            continue
        published = parse_published(item.get("published_at"))
        if not published and not include_undated:
            continue
        if published and (published < cutoff or published > now.astimezone(timezone.utc)):
            continue
        if event_type != "all" and str(item.get("event_type", "")) != event_type:
            continue
        if direction != "all" and str(item.get("direction", "")) != direction:
            continue
        haystack = " ".join(str(item.get(k) or "") for k in ("title", "summary", "source_name", "symbol")).casefold()
        if needle and needle not in haystack:
            continue
        output.append(item)
    output.sort(key=lambda x: (x.get("relevance_score", 0), x.get("published_at_parsed") or ""), reverse=True)
    return output


def build_recent_news_query(query: str, *, days: int = 30) -> str:
    """Add a recency window and finance-specific terms without duplicating them."""
    query = query.strip()
    if re.search(r"\bwhen:\s*\d+d\b", query, flags=re.I):
        return query
    return f'{query} (results OR shares OR NPA OR dividend OR RBI) when:{days}d'

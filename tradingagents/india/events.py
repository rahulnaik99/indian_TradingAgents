"""Sourced web/news research with Wigolo REST integration and RSS fallback.

The service never scrapes Moneycontrol. Prefer official sources and store source
URLs/publication timestamps for every event. RSS results are leads, not verified
facts; official-source verification is surfaced separately.
"""
from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import requests

from tradingagents.india.tracing import traced

WIGOLO_DEFAULT = "http://127.0.0.1:3333"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_event(title: str, summary: str = "", url: str = "") -> dict[str, Any]:
    text = f"{title} {summary}".casefold()
    host = urllib.parse.urlparse(url).netloc.casefold()
    official = any(domain in host for domain in ("rbi.org.in", "nseindia.com", "sebi.gov.in", "bseindia.com", ".gov.in"))
    rules: list[tuple[str, tuple[str, ...], str, str, float]] = [
        ("regulatory_action", ("rbi audit", "rbi audits", "regulatory action", "restriction", "penalty", "show cause", "fraud investigation", "sebi order"), "high", "uncertain", 0.82),
        ("monetary_policy", ("repo rate", "monetary policy", "policy rate", "cash reserve ratio", "crr", "statutory liquidity ratio", "slr"), "high", "uncertain", 0.78),
        ("quarterly_results", ("quarterly results", "q1 results", "q2 results", "q3 results", "q4 results", "net profit", "financial results", "earnings"), "high", "uncertain", 0.74),
        ("index_change", ("nifty 50 rebalancing", "index inclusion", "index exclusion", "added to nifty", "removed from nifty", "removed from banknifty", "removed from nifty bank", "excluded from banknifty", "listed out from banknifty", "index constituent change"), "high", "uncertain", 0.80),
        ("government_policy", ("government policy", "union budget", "ministry notification", "new regulation", "policy change", "cabinet approves"), "medium", "uncertain", 0.65),
        ("corporate_action", ("merger", "acquisition", "buyback", "dividend", "demerger", "stake sale"), "medium", "uncertain", 0.65),
    ]
    for category, terms, severity, direction, confidence in rules:
        if any(term in text for term in terms):
            if any(term in text for term in ("profit falls", "profit fall", "profit decline", "profit declined", "profit drops", "profit down", "p&l down", "p&l fell", "revenue down", "revenue declined", "earnings fell", "loss widens", "npa rises", "npa increased", "penalty", "fraud")):
                direction = "negative"
            elif any(term in text for term in ("profit rises", "profit jumps", "profit grows", "profit increased", "revenue grows", "earnings rise", "npa falls", "upgrade")):
                direction = "positive"
            return {"event_type": category, "severity": severity, "direction": direction,
                    "confidence": confidence if official else max(0.4, confidence - 0.15), "is_official_source": official}
    return {"event_type": "company_or_market_news", "severity": "low", "direction": "uncertain",
            "confidence": 0.35, "is_official_source": official}


class WebResearchError(RuntimeError):
    pass


class WebResearchService:
    def __init__(self, *, session: Any | None = None, timeout: float = 15.0):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.wigolo_url = os.getenv("WIGOLO_BASE_URL", WIGOLO_DEFAULT).rstrip("/")

    def _wigolo_post(self, tool: str, payload: dict[str, Any]) -> Any:
        headers = {"Content-Type": "application/json"}
        if os.getenv("WIGOLO_API_TOKEN"):
            headers["Authorization"] = f"Bearer {os.getenv('WIGOLO_API_TOKEN')}"
        response = self.session.post(f"{self.wigolo_url}/v1/{tool}", json=payload, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def health(self) -> dict[str, Any]:
        try:
            response = self.session.get(f"{self.wigolo_url}/health", timeout=min(self.timeout, 3.0))
            if response.ok:
                payload = response.json()
                return {"configured": True, "available": True, "base_url": self.wigolo_url, "status": payload}
        except (requests.RequestException, ValueError):
            pass
        return {"configured": True, "available": False, "base_url": self.wigolo_url,
                "fallback": "Google News RSS is used for search; direct page fetch/research needs Wigolo."}

    @traced("india.web_research.fetch")
    def fetch_url(self, url: str, *, max_content_chars: int = 12000) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Only absolute HTTPS URLs are accepted for web fetch")
        try:
            payload = self._wigolo_post("fetch", {"url": url, "max_content_chars": max(1000, min(max_content_chars, 30000))})
        except (requests.RequestException, ValueError) as exc:
            raise WebResearchError("Wigolo fetch is unavailable. Start `wigolo serve` and check WIGOLO_BASE_URL / WIGOLO_API_TOKEN.") from exc
        if not isinstance(payload, dict):
            raise WebResearchError("Wigolo fetch returned an unexpected response shape")
        return {"url": url, "title": payload.get("title"), "markdown": payload.get("markdown") or payload.get("content") or payload.get("text"),
                "metadata": payload.get("metadata", {}), "source": "wigolo", "raw_status": payload.get("status"),
                "note": "Review source publication date and terms before using extracted content."}

    @traced("india.web_research.research")
    def research(self, question: str, *, depth: str = "quick", max_sources: int = 12) -> dict[str, Any]:
        try:
            payload = self._wigolo_post("research", {"question": question, "depth": depth, "max_sources": max(1, min(max_sources, 50))})
            if isinstance(payload, dict):
                return {"question": question, "source": "wigolo", "result": payload,
                        "note": "Inspect source citations, gaps and publication dates before treating claims as verified."}
            raise WebResearchError("Wigolo research returned an unexpected response shape")
        except (requests.RequestException, ValueError) as exc:
            # RSS fallback keeps the feature usable when Wigolo is not installed/running.
            try:
                results = self.search(question, max_results=min(max_sources, 20))
            except WebResearchError:
                raise WebResearchError("Wigolo research unavailable and RSS fallback failed") from exc
            return {"question": question, "source": "rss_fallback", "results": results,
                    "note": "Multi-step synthesis unavailable; results are leads and must be verified."}

    @traced("india.web_research.search")
    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("query must not be empty")
        max_results = max(1, min(int(max_results), 25))
        try:
            headers = {"Content-Type": "application/json"}
            if os.getenv("WIGOLO_API_TOKEN"):
                headers["Authorization"] = f"Bearer {os.getenv('WIGOLO_API_TOKEN')}"
            response = self.session.post(
                f"{self.wigolo_url}/v1/search",
                json={"query": query, "max_results": min(max_results, 20), "category": "news", "search_depth": "balanced"},
                headers=headers, timeout=self.timeout,
            )
            if response.ok:
                payload = response.json()
                results = self._normalize_wigolo(payload, query)
                if results:
                    return results[:max_results]
        except (requests.RequestException, ValueError, TypeError):
            pass  # fallback below; do not hide the source in the result metadata
        return self._google_news_rss(query, max_results)

    @staticmethod
    def _normalize_wigolo(payload: Any, query: str) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            rows = payload.get("results") or payload.get("data") or payload.get("items") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            return []
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = row.get("url") or row.get("link")
            title = row.get("title") or row.get("name")
            if not url or not title:
                continue
            summary = row.get("snippet") or row.get("excerpt") or row.get("description") or ""
            freshness = row.get("freshness_signal") or {}
            published = row.get("published_at") or row.get("published") or row.get("date") or (freshness.get("published_date") if isinstance(freshness, dict) else None)
            classified = classify_event(str(title), str(summary), str(url))
            event_id = hashlib.sha256(f"{url}|{title}".encode()).hexdigest()[:24]
            normalized.append({"event_id": event_id, "query": query, "title": str(title), "summary": str(summary),
                               "source_name": row.get("source") or urllib.parse.urlparse(url).netloc,
                               "source_url": str(url), "published_at": published, "retrieved_at": _now(),
                               **classified, "provider": "wigolo"})
        return normalized

    def _google_news_rss(self, query: str, max_results: int) -> list[dict[str, Any]]:
        rss_url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        try:
            response = self.session.get(rss_url, timeout=self.timeout, headers={"User-Agent": "TradingAgents-India/0.2"})
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (requests.RequestException, ET.ParseError) as exc:
            raise WebResearchError(f"Web search failed; Wigolo unavailable and RSS fallback failed ({type(exc).__name__}).") from exc
        out = []
        for item in root.findall(".//item")[:max_results]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            description = re.sub(r"\s+", " ", description).strip()
            pubdate = item.findtext("pubDate")
            source_el = item.find("source")
            source_name = (source_el.text or "Google News") if source_el is not None else "Google News RSS"
            if not title or not link:
                continue
            classified = classify_event(title, description, link)
            out.append({"event_id": hashlib.sha256(f"{link}|{title}".encode()).hexdigest()[:24],
                        "query": query, "title": title, "summary": description,
                        "source_name": source_name, "source_url": link, "published_at": pubdate,
                        "retrieved_at": _now(), **classified, "provider": "google_news_rss",
                        "verification_status": "unverified_secondary_lead"})
        return out

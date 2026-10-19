"""Point-in-time fundamental facts, CSV import, and source-policy controls.

Official NSE/issuer filings are preferred. Moneycontrol collection is blocked
unless the operator confirms that written permission/licensing has been secured.
This module intentionally does not treat an unlicensed public HTML page as an API.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
from datetime import date, datetime, timezone
from typing import Any

from tradingagents.india.storage import IndiaStore
from tradingagents.india.tracing import traced

OFFICIAL_SOURCES = {
    "NSE financial results": "https://www.nseindia.com/companies-listing/corporate-filings-financial-results",
    "NSE integrated filing": "https://www.nseindia.com/companies-listing/corporate-integrated-filing",
    "NSE XBRL information": "https://www.nseindia.com/static/companies-listing/xbrl-information",
    "RBI DBIE": "https://dbieold.rbi.org.in/DBIE/",
    "Moneycontrol terms": "https://www.moneycontrol.com/cdata/termsofuse.php",
}


class FundamentalDataError(ValueError):
    pass


def source_guidance() -> dict[str, Any]:
    return {
        "primary": ["NSE financial results / XBRL", "Company investor-relations websites", "RBI and government sources for macro/policy"],
        "secondary": ["Moneycontrol only with explicit written permission/licence for automated use"],
        "sources": OFFICIAL_SOURCES,
        "moneycontrol_automated_collection_enabled": os.getenv("MONEYCONTROL_PERMISSION_CONFIRMED", "false").lower() in {"1", "true", "yes"},
        "note": "No Moneycontrol scraping is performed by default. Public page visibility does not itself grant automated collection rights.",
        "expected_csv_columns": ["symbol", "metric_code", "value", "unit", "currency", "period_start", "period_end", "period_type", "scope", "source_name", "source_url", "published_at", "filing_status", "revision_of"],
    }


@traced("india.fundamentals.import_csv")
def import_facts_csv(text: str, store: IndiaStore) -> dict[str, Any]:
    """Import already-obtained, sourced facts from CSV without guessing values."""
    if not text.strip():
        raise FundamentalDataError("CSV body is empty")
    reader = csv.DictReader(io.StringIO(text))
    required = {"symbol", "metric_code", "value", "unit", "period_start", "period_end", "period_type", "scope", "source_name", "source_url", "published_at"}
    missing = required - set(reader.fieldnames or ())
    if missing:
        raise FundamentalDataError("Missing required columns: " + ", ".join(sorted(missing)))
    inserted = 0
    errors = []
    for line_no, row in enumerate(reader, start=2):
        try:
            symbol = row["symbol"].strip().upper()
            if not symbol or not symbol.replace("&", "").replace("-", "").replace("_", "").isalnum():
                raise ValueError("invalid symbol")
            value = float(row["value"])
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError("value must be finite")
            period_start, period_end = date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"])
            if period_end < period_start:
                raise ValueError("period_end precedes period_start")
            published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
            if published.tzinfo is None or published.utcoffset() is None:
                raise ValueError("published_at must include a timezone offset")
            retrieved = datetime.now(timezone.utc)
            status = (row.get("filing_status") or "original").strip().lower()
            if status not in {"original", "revised", "restated", "unknown"}:
                raise ValueError("invalid filing_status")
            if status in {"revised", "restated"} and not (row.get("revision_of") or "").strip():
                raise ValueError("revised/restated rows require revision_of")
            scope = row["scope"].strip().lower()
            if scope not in {"standalone", "consolidated", "unspecified"}:
                raise ValueError("scope must be standalone, consolidated or unspecified")
            fact = {
                "symbol": symbol, "metric_code": row["metric_code"].strip(), "value": value,
                "unit": row["unit"].strip(), "currency": (row.get("currency") or "").strip() or None,
                "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
                "period_type": row["period_type"].strip(), "scope": scope,
                "source_name": row["source_name"].strip(), "source_url": row["source_url"].strip(),
                "published_at": published.isoformat(), "retrieved_at": retrieved.isoformat(),
                "filing_status": status, "revision_of": (row.get("revision_of") or "").strip() or None,
            }
            for field in ("metric_code", "unit", "period_type", "scope", "source_name", "source_url"):
                if not fact[field]:
                    raise ValueError(f"{field} is empty")
            fact["fact_id"] = hashlib.sha256(("|".join(str(fact[k]) for k in ("symbol", "metric_code", "period_end", "scope", "source_url", "published_at", "value"))).encode()).hexdigest()[:32]
            store.save_fact(fact)
            inserted += 1
        except (KeyError, ValueError, TypeError) as exc:
            errors.append({"line": line_no, "error": str(exc)})
    return {"inserted": inserted, "errors": errors, "point_in_time_rule": "Facts are available only from published_at onward."}


def facts_as_of(facts: list[dict[str, Any]], as_of: datetime) -> list[dict[str, Any]]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return [f for f in facts if datetime.fromisoformat(f["published_at"]) <= as_of]


def parse_xbrl_facts(xml_text: str, *, symbol: str, source_url: str, published_at: str, scope: str = "consolidated") -> dict[str, Any]:
    """Extract numeric facts from a supplied XBRL instance without guessing taxonomy mappings.

    The response preserves original element names and context IDs. A separate
    reviewed taxonomy mapping is required before facts are used as canonical metrics.
    """
    import xml.etree.ElementTree as ET
    from datetime import timezone

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FundamentalDataError("XBRL/XML document could not be parsed") from exc
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FundamentalDataError("published_at must be an ISO timestamp") from exc
    if published.tzinfo is None or published.utcoffset() is None:
        raise FundamentalDataError("published_at must include a timezone offset")
    contexts: dict[str, dict[str, Any]] = {}
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1].lower()
        if local == "context":
            context_id = element.attrib.get("id")
            if not context_id:
                continue
            values = {}
            for child in element.iter():
                child_local = child.tag.rsplit("}", 1)[-1].lower()
                if child_local in {"startdate", "enddate", "instant"} and child.text:
                    values[child_local] = child.text.strip()
            contexts[context_id] = values
    output = []
    for element in root.iter():
        context_id = element.attrib.get("contextRef") or element.attrib.get("contextref")
        if not context_id or context_id not in contexts or not (element.text or "").strip():
            continue
        raw = (element.text or "").strip().replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if value != value or value in (float("inf"), float("-inf")):
            continue
        period = contexts[context_id]
        end = period.get("enddate") or period.get("instant")
        start = period.get("startdate") or end
        if not start or not end:
            continue
        try:
            start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            continue
        output.append({"symbol": symbol.upper(), "reported_metric_name": element.tag.rsplit("}", 1)[-1],
                       "context_id": context_id, "value": value, "period_start": start_date.isoformat(),
                       "period_end": end_date.isoformat(), "scope": scope, "source_url": source_url,
                       "published_at": published.isoformat(), "mapping_status": "unmapped_taxonomy_fact"})
    return {"symbol": symbol.upper(), "source_url": source_url, "fact_count": len(output), "facts": output,
            "note": "Raw XBRL facts preserve original taxonomy names. Map and validate taxonomy, units, decimals and reporting scope before treating them as canonical financial metrics."}


def summarize_facts(facts: list[dict[str, Any]], *, as_of: datetime | None = None) -> dict[str, Any]:
    """Build point-in-time QoQ/YoY comparisons from already sourced facts.

    Comparisons are computed only between matching metric, unit, scope and period
    types. Percentage changes use the absolute previous value as denominator and
    are labeled accordingly; a zero prior value produces no percentage change.
    """
    from collections import defaultdict

    cutoff = as_of or datetime.now(timezone.utc)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    visible = [f for f in facts if datetime.fromisoformat(f["published_at"]) <= cutoff]
    groups: dict[tuple[str, str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for fact in visible:
        key = (fact["metric_code"], fact["scope"], fact["unit"], fact["period_type"])
        period_end = fact["period_end"]
        current = groups[key].get(period_end)
        if current is None or datetime.fromisoformat(fact["published_at"]) > datetime.fromisoformat(current["published_at"]):
            groups[key][period_end] = fact
    summaries = []
    for (metric, scope, unit, period_type), by_period in groups.items():
        series = sorted(by_period.values(), key=lambda f: f["period_end"])
        for index, fact in enumerate(series):
            end = date.fromisoformat(fact["period_end"])
            qoq = yoy = None
            if period_type.casefold() in {"quarter", "quarterly", "q"}:
                prior_candidates = []
                year_candidates = []
                for prior in series[:index]:
                    days = (end - date.fromisoformat(prior["period_end"])).days
                    if 60 <= days <= 120:
                        prior_candidates.append((days, prior))
                    if 330 <= days <= 400:
                        year_candidates.append((days, prior))
                qoq = prior_candidates[-1][1] if prior_candidates else None
                yoy = year_candidates[-1][1] if year_candidates else None
            elif period_type.casefold() in {"annual", "year", "yearly"}:
                year_candidates = [(days, prior) for prior in series[:index]
                                   if 330 <= (days := (end - date.fromisoformat(prior["period_end"])).days) <= 400]
                yoy = year_candidates[-1][1] if year_candidates else None

            def change(previous: dict[str, Any] | None) -> dict[str, Any] | None:
                if previous is None:
                    return None
                old = float(previous["value"])
                current_value = float(fact["value"])
                return {"previous_period_end": previous["period_end"], "previous_value": old,
                        "absolute_change": round(current_value - old, 6),
                        "percentage_change": round((current_value - old) / abs(old) * 100.0, 4) if old != 0 else None,
                        "percentage_basis": "absolute_previous_value", "previous_published_at": previous["published_at"]}

            summaries.append({"symbol": fact["symbol"], "metric_code": metric, "value": fact["value"],
                              "unit": unit, "currency": fact.get("currency"), "scope": scope,
                              "period_type": period_type, "period_start": fact["period_start"],
                              "period_end": fact["period_end"], "published_at": fact["published_at"],
                              "source_name": fact["source_name"], "source_url": fact["source_url"],
                              "filing_status": fact["filing_status"], "qoq": change(qoq), "yoy": change(yoy)})
    summaries.sort(key=lambda item: (item["period_end"], item["metric_code"]), reverse=True)
    return {"as_of": cutoff.isoformat(), "facts_count": len(summaries), "facts": summaries,
            "note": "Only facts published by as_of are included. QoQ/YoY comparisons require matching metric, unit, scope and period type; percent changes use the absolute previous value as denominator."}


YAHOO_SNAPSHOT_METRICS = {
    "trailingPE": ("PE_RATIO", "ratio", None),
    "forwardPE": ("FORWARD_PE", "ratio", None),
    "priceToBook": ("PB_RATIO", "ratio", None),
    "returnOnEquity": ("ROE", "fraction", None),
    "debtToEquity": ("DEBT_TO_EQUITY", "percent", None),
    "trailingEps": ("EPS", "INR_per_share", "INR"),
    "dividendYield": ("DIVIDEND_YIELD", "percent", None),
    "marketCap": ("MARKET_CAP", "INR", "INR"),
}


def refresh_yahoo_snapshot(symbol: str, store: IndiaStore) -> dict[str, Any]:
    """Fetch a current Yahoo Finance snapshot for an NSE equity and persist sourced observations.

    These are retrieval-time snapshots, not historical filing facts. `published_at` is
    set to the retrieval timestamp solely to enforce a point-in-time cutoff for this
    observed snapshot; payload metadata explicitly records that it is not a filing date.
    Missing provider fields are omitted rather than filled with zero.
    """
    normalized = symbol.strip().upper()
    if not normalized or not normalized.replace("&", "").replace("-", "").replace("_", "").isalnum():
        raise FundamentalDataError("Invalid NSE symbol")
    try:
        import yfinance as yf
    except ImportError as exc:
        raise FundamentalDataError("yfinance is not installed in this environment. Run `./start.sh` to install project dependencies.") from exc
    ticker = f"{normalized}.NS"
    try:
        info = yf.Ticker(ticker).get_info()
    except Exception as exc:
        raise FundamentalDataError(f"Yahoo Finance request failed ({type(exc).__name__}); no facts were changed.") from exc
    if not isinstance(info, dict) or not info:
        raise FundamentalDataError(f"Yahoo Finance returned no snapshot for {ticker}; no facts were changed.")

    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    as_of_date = now.date().isoformat()
    source_url = f"https://finance.yahoo.com/quote/{ticker}/"
    inserted = []
    for yahoo_key, (metric_code, unit, currency) in YAHOO_SNAPSHOT_METRICS.items():
        raw = info.get(yahoo_key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            continue
        # yfinance's dividendYield field is a fraction (e.g. 0.025 means 2.5%).
        # Store it in percentage points so the UI can display 2.5% without
        # accidentally showing 0.025%.
        if yahoo_key == "dividendYield":
            value *= 100.0
        fact = {
            "symbol": normalized, "metric_code": metric_code, "value": value, "unit": unit,
            "currency": currency, "period_start": as_of_date, "period_end": as_of_date,
            "period_type": "snapshot", "scope": "unspecified", "source_name": "Yahoo Finance (yfinance)",
            "source_url": source_url, "published_at": stamp, "retrieved_at": stamp,
            "filing_status": "unknown", "revision_of": None, "as_of_date": as_of_date,
            "observation_type": "retrieval_time_snapshot", "provider_symbol": ticker,
            "note": "Current provider snapshot, not an official filing or historical point-in-time statement.",
            "yahoo_industry": info.get("industry") if isinstance(info.get("industry"), str) else None,
            "yahoo_sector": info.get("sector") if isinstance(info.get("sector"), str) else None,
        }
        fact["fact_id"] = hashlib.sha256((f"{normalized}|{metric_code}|{as_of_date}|{source_url}|{value}").encode()).hexdigest()[:32]
        store.save_fact(fact)
        inserted.append({"metric_code": metric_code, "value": value, "unit": unit, "as_of_date": as_of_date})
    return {"symbol": normalized, "provider_symbol": ticker, "source_name": "Yahoo Finance (yfinance)",
            "source_url": source_url, "as_of_date": as_of_date, "retrieved_at": stamp,
            "yahoo_industry": info.get("industry") if isinstance(info.get("industry"), str) else None,
            "yahoo_sector": info.get("sector") if isinstance(info.get("sector"), str) else None,
            "inserted": len(inserted), "facts": inserted,
            "unavailable_metrics": [code for _, (code, _, _) in YAHOO_SNAPSHOT_METRICS.items() if code not in {x["metric_code"] for x in inserted}],
            "note": "Values are current Yahoo Finance snapshots, not official filings. Bank-specific NIM and GNPA are not fetched; import them manually from validated primary sources."}

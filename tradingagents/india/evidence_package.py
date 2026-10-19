"""Build a compact, point-in-time evidence package for the original agents.

Only evidence with a parseable publication timestamp at or before ``as_of`` is
eligible for historical agent context. Retrieval time is provenance, not a
substitute for publication time.
"""
from __future__ import annotations

from tradingagents.india.fundamentals_view import is_statement_fact, statements_from_facts
from tradingagents.india.financial_flags import evaluate_financial_flags

import json
import csv
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

SCHEMA_VERSION = "1.3"


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _research_sources(research: dict[str, Any] | None) -> tuple[str | None, list[dict[str, Any]], str | None]:
    if not isinstance(research, dict):
        return None, [], None
    source = research.get("source")
    raw = research.get("result") if isinstance(research.get("result"), dict) else {}
    rows = raw.get("sources") or raw.get("citations") or raw.get("references") or []
    normalized: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows[:12]:
            if isinstance(row, str):
                normalized.append({"url": row, "title": None, "published_at": None})
            elif isinstance(row, dict):
                url = row.get("url") or row.get("link") or row.get("source_url")
                if not url:
                    continue
                normalized.append({
                    "url": str(url)[:1500],
                    "title": str(row.get("title") or row.get("name") or "")[:300] or None,
                    "published_at": row.get("published_at") or row.get("published_date") or row.get("date"),
                    "source_name": row.get("source") or row.get("domain"),
                })
    brief = raw.get("brief") or raw.get("summary") or raw.get("answer") or raw.get("content")
    if brief is None and source == "rss_fallback":
        brief = "Multi-step synthesis was unavailable; RSS search results are unverified research leads."
    return (str(source) if source is not None else None, normalized, str(brief)[:7000] if brief else None)


def build_evidence_package(*, symbol: str, as_of: str, technical: dict[str, Any],
                           events: list[dict[str, Any]], facts: list[dict[str, Any]],
                           research: dict[str, Any] | None = None,
                           calendar_events: list[dict[str, Any]] | None = None,
                           market_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a bounded package plus explicit point-in-time exclusions/warnings."""
    cutoff = parse_timestamp(as_of)
    if cutoff is None:
        raise ValueError("as_of must be an ISO timestamp with a timezone offset")

    eligible_events: list[dict[str, Any]] = []
    excluded_event_unknown = 0
    excluded_event_future = 0
    for event in events:
        published = parse_timestamp(event.get("published_at"))
        if published is None:
            excluded_event_unknown += 1
            continue
        if published > cutoff:
            excluded_event_future += 1
            continue
        eligible_events.append({**event, "published_at_utc": published.isoformat()})
    eligible_events.sort(key=lambda row: row["published_at_utc"], reverse=True)

    # Calendar rows are scheduled-event records, not news articles. Their event
    # date may be in the future, so use created_at as the earliest known-at time
    # and keep event_date separate from publication time. This avoids leaking a
    # calendar entry into a historical run if it was first retrieved later.
    calendar_rows: list[dict[str, Any]] = []
    excluded_calendar_unknown = 0
    excluded_calendar_future = 0
    excluded_calendar_out_of_window = 0
    local_as_of = datetime.fromisoformat(as_of.strip().replace("Z", "+00:00"))
    as_of_day = local_as_of.date()
    window_start = as_of_day.fromordinal(as_of_day.toordinal() - 30)
    window_end = as_of_day.fromordinal(as_of_day.toordinal() + 90)
    for event in calendar_events or []:
        if not isinstance(event, dict):
            continue
        assets = event.get("assets") if isinstance(event.get("assets"), list) else ["NSE"]
        if "NSE" not in [str(asset).upper() for asset in assets]:
            continue
        event_symbol = str(event.get("symbol") or "").strip().upper()
        if event_symbol and event_symbol != symbol.strip().upper():
            continue
        known_at = parse_timestamp(event.get("created_at") or event.get("known_at"))
        if known_at is None:
            excluded_calendar_unknown += 1
            continue
        if known_at > cutoff:
            excluded_calendar_future += 1
            continue
        try:
            event_day = datetime.fromisoformat(str(event.get("date") or "").strip()[:10]).date()
        except ValueError:
            excluded_calendar_out_of_window += 1
            continue
        if event_day < window_start or event_day > window_end:
            excluded_calendar_out_of_window += 1
            continue
        calendar_rows.append({**event, "event_date": event_day.isoformat(), "known_at_utc": known_at.isoformat()})
    calendar_rows.sort(key=lambda row: (row["event_date"], row.get("title", "")))

    eligible_facts: list[dict[str, Any]] = []
    excluded_fact_unknown = 0
    excluded_fact_future = 0
    for fact in facts:
        published = parse_timestamp(fact.get("published_at"))
        if published is None:
            excluded_fact_unknown += 1
            continue
        if published > cutoff:
            excluded_fact_future += 1
            continue
        # Preserve metric, value, unit/currency, period, scope, filing status,
        # source and revision metadata for the analyst rather than flattening it.
        eligible_facts.append({**fact, "published_at_utc": published.isoformat()})
    eligible_facts.sort(key=lambda row: (str(row.get("period_end", "")), row["published_at_utc"]), reverse=True)
    # Provider statement facts are numerous (about 230 per symbol). Keep them out of the flat, truncated
    # fact list and give the analysts a compact metric x period table instead.
    statement_rows = [row for row in eligible_facts if is_statement_fact(row)]
    financial_statements = statements_from_facts(statement_rows)
    financial_health_flags = None
    if financial_statements is not None:
        def _is_financial_symbol(sym: str) -> bool:
            universe = Path(__file__).resolve().parents[2] / "data" / "nifty_500_constituents.csv"
            try:
                with universe.open(encoding="utf-8-sig", newline="") as stream:
                    for row in csv.DictReader(stream):
                        if (row.get("symbol") or "").strip().upper() == sym.upper():
                            label = " ".join((row.get("industry") or "", row.get("sector") or "")).lower()
                            return any(term in label for term in ("financial", "bank", "insurance", "capital market", "lending"))
            except OSError:
                pass
            return False
        annual_view = financial_statements.get("annual", {"periods": [], "sections": {}})
        quarterly_view = financial_statements.get("quarterly", {"periods": [], "sections": {}})
        financial_health_flags = evaluate_financial_flags(
            {**annual_view, "symbol": symbol.upper()}, {**quarterly_view, "symbol": symbol.upper()},
            is_financial=_is_financial_symbol(symbol))
        eligible_facts = [row for row in eligible_facts if not is_statement_fact(row)]

    timeframe_reports = technical.get("timeframes", {}) if isinstance(technical, dict) else {}
    technical_warnings: list[str] = []
    if not isinstance(timeframe_reports, dict):
        timeframe_reports = {}
        technical_warnings.append("Technical report did not contain a valid timeframes object.")
    for timeframe in ("1d", "4h", "1h"):
        report = timeframe_reports.get(timeframe)
        if not isinstance(report, dict):
            technical_warnings.append(f"{timeframe}: report missing.")
        elif report.get("status") != "ok":
            technical_warnings.append(f"{timeframe}: status={report.get('status', 'unknown')}, candles={report.get('count', 0)}.")
    if technical.get("source_provider"):
        market_source = technical.get("source_provider")
    else:
        market_source = "FYERS historical candles (provider metadata on aggregate report may be absent)"

    research_source, research_sources, research_brief = _research_sources(research)
    eligible_research_sources: list[dict[str, Any]] = []
    excluded_research_unknown = 0
    excluded_research_future = 0
    for source in research_sources:
        published = parse_timestamp(source.get("published_at"))
        if published is None:
            excluded_research_unknown += 1
            continue
        if published > cutoff:
            excluded_research_future += 1
            continue
        eligible_research_sources.append({**source, "published_at_utc": published.isoformat()})
    # A synthesized brief may blend claims across sources. Only pass it to agents
    # when every extracted citation has a known publication timestamp at/before
    # the requested cutoff; otherwise pass dated citations only and disclose the gap.
    if not research_sources or len(eligible_research_sources) != len(research_sources):
        research_brief = None
    research_sources = eligible_research_sources
    warnings = list(technical_warnings)
    if not isinstance(market_context, dict) or market_context.get("status") != "available":
        warnings.append("Market-movers context was unavailable at as_of; no market-wide ranking or breadth conclusion was used.")
    if not eligible_events:
        warnings.append("No news/event evidence with a parseable publication timestamp at or before as_of was eligible.")
    if not calendar_rows:
        warnings.append("No relevant NSE calendar events known by as_of were eligible in the -30/+90 day window.")
    if not eligible_facts:
        warnings.append("No financial facts with a parseable publication timestamp at or before as_of were eligible.")
    if excluded_event_unknown or excluded_event_future:
        warnings.append(f"Excluded events: {excluded_event_unknown} missing/invalid publication timestamp; {excluded_event_future} published after as_of.")
    if excluded_calendar_unknown or excluded_calendar_future or excluded_calendar_out_of_window:
        warnings.append(f"Excluded calendar events: {excluded_calendar_unknown} missing/invalid known-at timestamp; {excluded_calendar_future} first known after as_of; {excluded_calendar_out_of_window} invalid/outside -30/+90 day event-date window.")
    if excluded_fact_unknown or excluded_fact_future:
        warnings.append(f"Excluded facts: {excluded_fact_unknown} missing/invalid publication timestamp; {excluded_fact_future} published after as_of.")
    if research and research_source != "wigolo":
        warnings.append("Deep research synthesis was not returned by Wigolo; any fallback results are leads, not verified facts.")
    if research and research_source == "wigolo" and not research_sources:
        warnings.append("Wigolo returned no citations with a verifiable publication timestamp at/before as_of; its synthesized brief was withheld from the agent context.")
    if excluded_research_unknown or excluded_research_future:
        warnings.append(f"Excluded Wigolo citations: {excluded_research_unknown} missing/invalid publication timestamp; {excluded_research_future} published after as_of. The synthesized brief was withheld because it may mix unsafe timestamps.")

    # A compact, explicit fundamental digest makes the existing Fundamental
    # Analysis evidence visible to downstream agents without inventing a new
    # LLM score. Keep source, periods, flags, and missing-data state together.
    fundamental_analysis = {
        "status": "available" if financial_statements or eligible_facts else "unavailable",
        "source": (financial_statements or {}).get("source_name") if financial_statements else None,
        "statement_periods": {
            "annual": (financial_statements or {}).get("annual", {}).get("periods", []),
            "quarterly": (financial_statements or {}).get("quarterly", {}).get("periods", []),
        },
        "financial_health_flags": financial_health_flags,
        "valuation_facts": [
            row for row in eligible_facts
            if any(token in str(row.get("metric_code", "")).casefold()
                   for token in ("pe_ratio", "price_earnings", "pb_ratio", "price_book", "valuation"))
        ][:20],
        "other_sourced_fact_count": len(eligible_facts),
        "note": "Fundamental health flags are transparent rules over provider statements; missing values are unknown, not healthy. Valuation metrics are included only when present in sourced facts.",
    }

    package = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol.upper(),
        "exchange": "NSE",
        "as_of": cutoff.isoformat(),
        "market_data": {
            "provider": market_source,
            "technical_snapshot": technical,
            "point_in_time_rule": "Only complete candles with close_time <= as_of should be used; upstream report is supplied as calculated evidence, not as an order signal.",
        },
        "market_context": market_context or {
            "status": "unavailable",
            "reason": "No eligible completed scanner snapshot was available at the requested as_of time.",
            "source": "local completed scanner jobs",
        },
        "news_and_events": eligible_events[:30],
        "calendar_events": calendar_rows[:100],
        "financial_facts": eligible_facts[:60],
        "financial_statements": financial_statements,
        "financial_health_flags": financial_health_flags,
        "fundamental_analysis": fundamental_analysis,
        "web_research": {
            "provider": research_source,
            "brief": research_brief,
            "sources": research_sources,
            "excluded_unknown_timestamp_count": excluded_research_unknown,
            "excluded_future_timestamp_count": excluded_research_future,
            "note": research.get("note") if isinstance(research, dict) else None,
        },
        "data_quality": {
            "eligible_event_count": len(eligible_events),
            "excluded_event_unknown_timestamp_count": excluded_event_unknown,
            "excluded_event_future_timestamp_count": excluded_event_future,
            "eligible_calendar_event_count": len(calendar_rows),
            "excluded_calendar_unknown_timestamp_count": excluded_calendar_unknown,
            "excluded_calendar_future_timestamp_count": excluded_calendar_future,
            "excluded_calendar_out_of_window_count": excluded_calendar_out_of_window,
            "eligible_fact_count": len(eligible_facts),
            "excluded_fact_unknown_timestamp_count": excluded_fact_unknown,
            "excluded_fact_future_timestamp_count": excluded_fact_future,
            "warnings": warnings,
        },
        "agent_instructions": [
            "Treat this package as shared evidence, not as a pre-decided conclusion.",
            "Market regime and breadth are descriptive context from the configured scanner subset, not all NSE stocks and not a standalone trade signal.",
            "If market breadth conflicts with the selected stock trend, explicitly describe the divergence and lower confidence; do not force alignment.",
            "Use only evidence published at or before as_of for historical claims.",
            "Cite source URLs and publication timestamps where available.",
            "Keep reported facts separate from interpretation; do not invent values or dates.",
            "If evidence conflicts with your tools, explain the conflict and identify the source of each value.",
            "Treat secondary search results as leads unless the underlying source is verified.",
            "Explicitly report missing data and all data_quality warnings.",
            "Do not place orders or describe this research package as a trade instruction.",
        ],
    }
    return package


def serialize_agent_context(package: dict[str, Any], *, max_chars: int = 24000) -> str:
    """Serialize a point-in-time package within a strict, progressively reduced budget.

    Large scanner snapshots and statement tables can vary considerably by symbol.
    Compaction therefore proceeds in stages and has a minimal evidence-preserving
    fallback rather than failing the entire Full Analysis request.
    """
    def dump(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))

    if max_chars < 1000:
        raise ValueError("max_chars must be at least 1000 to preserve evidence metadata")
    payload = dump(package)
    if len(payload) <= max_chars:
        return payload

    reduced = dict(package)
    # Stage 1: bound high-cardinality evidence lists and long narrative text.
    reduced["news_and_events"] = list(package.get("news_and_events", []))[:12]
    reduced["calendar_events"] = list(package.get("calendar_events", []))[:20]
    reduced["financial_facts"] = list(package.get("financial_facts", []))[:15]
    research = dict(package.get("web_research", {}))
    research["sources"] = list(research.get("sources", []))[:4]
    research["brief"] = str(research.get("brief") or "")[:1200]
    reduced["web_research"] = research

    def compact_market_context(value: Any, *, very_small: bool = False) -> Any:
        if not isinstance(value, dict):
            return value
        keep = ("status", "as_of", "source", "snapshot_age_hours", "universe", "market_breadth",
                "selected_symbol", "selected_stock", "warnings", "reason", "job_id")
        out = {k: value[k] for k in keep if k in value}
        for key, limit in (("top_gainers", 3), ("top_losers", 3), ("highest_volume", 3)):
            rows = value.get(key)
            if isinstance(rows, list):
                out[key] = rows[:(1 if very_small else limit)]
        if isinstance(out.get("selected_stock"), dict):
            selected = out["selected_stock"]
            fields = ("symbol", "name", "close", "change_pct", "volume", "multi_timeframe_bias", "trend_score", "status")
            out["selected_stock"] = {k: selected[k] for k in fields if k in selected}
        return out

    reduced["market_context"] = compact_market_context(package.get("market_context"))
    statements = package.get("financial_statements")
    if isinstance(statements, dict):
        compact_statements = {k: v for k, v in statements.items() if k not in ("quarterly", "annual")}
        annual = statements.get("annual")
        if isinstance(annual, dict):
            compact_statements["annual"] = {"periods": list(annual.get("periods", []))[:4],
                "sections": annual.get("sections", {})}
        reduced["financial_statements"] = compact_statements or None
    # Fundamental digest and flags are intentionally retained in early stages.
    reduced["fundamental_analysis"] = package.get("fundamental_analysis", {})
    reduced["financial_health_flags"] = package.get("financial_health_flags")
    payload = dump(reduced)
    if len(payload) <= max_chars:
        return payload

    # Stage 2: trim large nested values while retaining all timeframes and their
    # core indicators, fundamental flags, provenance, cutoff, and quality counts.
    technical = package.get("market_data", {}).get("technical_snapshot", {})
    compact_tfs = {}
    if isinstance(technical, dict):
        for tf, row in (technical.get("timeframes", {}) or {}).items():
            if isinstance(row, dict):
                keys = ("status", "count", "latest_open_time", "latest_close_time", "latest_close", "trend",
                        "trend_score", "ema20", "ema50", "ema100", "ema200", "rsi14", "atr14",
                        "session_vwap", "support_20", "resistance_20", "relative_volume_20", "market_structure")
                compact_tfs[tf] = {k: row[k] for k in keys if k in row}
    reduced["market_data"] = {"provider": package.get("market_data", {}).get("provider"),
        "technical_snapshot": {"symbol": package.get("symbol"),
            "multi_timeframe_bias": technical.get("multi_timeframe_bias") if isinstance(technical, dict) else None,
            "aggregate_trend_score": technical.get("aggregate_trend_score") if isinstance(technical, dict) else None,
            "timeframes": compact_tfs}}
    reduced["news_and_events"] = reduced.get("news_and_events", [])[:6]
    reduced["calendar_events"] = reduced.get("calendar_events", [])[:8]
    reduced["financial_facts"] = reduced.get("financial_facts", [])[:8]
    reduced["market_context"] = compact_market_context(package.get("market_context"), very_small=True)
    if isinstance(reduced.get("financial_statements"), dict):
        fs = reduced["financial_statements"]
        annual = fs.get("annual")
        if isinstance(annual, dict):
            sections = annual.get("sections", {})
            fs["annual"] = {"periods": list(annual.get("periods", []))[:3],
                "sections": {k: v[:8] for k, v in sections.items() if isinstance(v, list)}}
    fa = package.get("fundamental_analysis", {})
    if isinstance(fa, dict):
        reduced["fundamental_analysis"] = {k: fa.get(k) for k in (
            "status", "source", "statement_periods", "financial_health_flags", "other_sourced_fact_count", "note") if k in fa}
        if isinstance(reduced["fundamental_analysis"].get("valuation_facts"), list):
            reduced["fundamental_analysis"]["valuation_facts"] = reduced["fundamental_analysis"]["valuation_facts"][:5]
    payload = dump(reduced)
    if len(payload) <= max_chars:
        return payload

    # Stage 3: compact fallback. Keep auditability and core analysis evidence,
    # but omit bulky raw lists/tables instead of aborting Full Analysis.
    dq = package.get("data_quality", {})
    fallback = {
        "schema_version": package.get("schema_version"), "symbol": package.get("symbol"),
        "exchange": package.get("exchange"), "as_of": package.get("as_of"),
        "market_data": reduced.get("market_data", {}),
        "market_context": compact_market_context(package.get("market_context"), very_small=True),
        "news_and_events": reduced.get("news_and_events", [])[:3],
        "calendar_events": reduced.get("calendar_events", [])[:3],
        "financial_facts": reduced.get("financial_facts", [])[:3],
        "financial_statements": None,
        "financial_health_flags": package.get("financial_health_flags"),
        "fundamental_analysis": reduced.get("fundamental_analysis", {}),
        "web_research": {"provider": research.get("provider"), "sources": research.get("sources", [])[:2],
                          "brief": str(research.get("brief") or "")[:400],
                          "note": "Context budget fallback applied; consult source records for full evidence."},
        "data_quality": {k: dq.get(k) for k in (
            "eligible_event_count", "excluded_event_unknown_timestamp_count", "excluded_event_future_timestamp_count",
            "eligible_calendar_event_count", "excluded_calendar_unknown_timestamp_count", "excluded_calendar_future_timestamp_count",
            "eligible_fact_count", "excluded_fact_unknown_timestamp_count", "excluded_fact_future_timestamp_count", "warnings") if k in dq},
        "agent_instructions": list(package.get("agent_instructions", []))[:6],
        "context_compaction": {"applied": True, "level": "minimal", "original_chars": len(dump(package))},
    }
    payload = dump(fallback)
    if len(payload) <= max_chars:
        return payload
    # Extremely small custom budgets can still be satisfied without losing the
    # symbol/cutoff/provider and an explicit statement that detail was omitted.
    minimal = {"schema_version": package.get("schema_version"), "symbol": package.get("symbol"),
        "exchange": package.get("exchange"), "as_of": package.get("as_of"),
        "market_data": reduced.get("market_data", {}),
        "data_quality": {"warnings": dq.get("warnings", [])},
        "context_compaction": {"applied": True, "level": "minimal", "original_chars": len(dump(package)),
                               "note": "Large evidence fields omitted to fit agent context."}}
    payload = dump(minimal)
    if len(payload) > max_chars:
        raise ValueError("max_chars is too small for the minimum evidence metadata")
    return payload

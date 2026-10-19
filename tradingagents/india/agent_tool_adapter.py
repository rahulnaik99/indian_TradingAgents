"""Scoped adapters that route original analyst tools to the shared India evidence.

The adapter is enabled only while a TradingAgents graph is invoked with an India
 evidence package. Non-India/CLI runs retain the upstream provider behavior. When
India evidence is missing, the adapter fails closed rather than silently falling
back to Yahoo data that may disagree with the India dashboard.
"""
from __future__ import annotations

import contextvars
import csv
import io
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterator

_ACTIVE_PACKAGE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "tradingagents_india_evidence_package", default=None
)


@contextmanager
def use_india_evidence(package: dict[str, Any] | str | None) -> Iterator[None]:
    if isinstance(package, str):
        try:
            package = json.loads(package)
        except (TypeError, ValueError):
            package = None
    token = _ACTIVE_PACKAGE.set(package if isinstance(package, dict) else None)
    try:
        yield
    finally:
        _ACTIVE_PACKAGE.reset(token)


def is_india_evidence_package(package: Any) -> bool:
    """Return whether a decoded package is a supported NSE evidence package."""
    return isinstance(package, dict) and str(package.get("exchange", "")).upper() == "NSE" and bool(package.get("symbol"))


def active_package() -> dict[str, Any] | None:
    package = _ACTIVE_PACKAGE.get()
    return package if is_india_evidence_package(package) else None


def _parse_aware_timestamp(value: Any) -> datetime | None:
    """Parse an ISO timestamp only when it includes a timezone offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _package_cutoff(package: dict[str, Any], requested_end: str | None = None) -> datetime | None:
    """Return the exact package cutoff, optionally narrowed to an earlier date."""
    cutoff = _parse_aware_timestamp(package.get("as_of"))
    if cutoff is None or not requested_end:
        return cutoff
    try:
        requested_day = _as_date(requested_end)
    except (TypeError, ValueError):
        return cutoff
    local_cutoff = cutoff.astimezone(datetime.fromisoformat(str(package["as_of"]).replace("Z", "+00:00")).tzinfo)
    if requested_day < local_cutoff.date():
        local_end = datetime.combine(requested_day, time.max, tzinfo=local_cutoff.tzinfo)
        return min(cutoff, local_end.astimezone(timezone.utc))
    return cutoff


def _published_timestamp(row: dict[str, Any]) -> datetime | None:
    # Prefer normalized UTC timestamp, then source timestamp. Date-only and naive
    # values are deliberately ineligible for point-in-time tool responses.
    return _parse_aware_timestamp(row.get("published_at_utc") or row.get("published_at"))


def _not_available(tool_name: str, reason: str) -> str:
    return (
        f"INDIA_DATA_UNAVAILABLE [{tool_name}]: {reason}. "
        "No alternate market-data provider was silently substituted. "
        "Use the shared evidence package and disclose the missing source."
    )


def _as_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _events_text(package: dict[str, Any], *, start_date: str | None = None, end_date: str | None = None) -> str:
    rows = package.get("news_and_events", [])
    calendar_rows = package.get("calendar_events", [])
    try:
        start = _as_date(start_date) if start_date else None
    except (TypeError, ValueError):
        start = None
    cutoff = _package_cutoff(package, end_date)
    if cutoff is None:
        return _not_available("get_news", "evidence package has no timezone-aware analysis cutoff")
    try:
        package_as_of = datetime.fromisoformat(str(package.get("as_of")).replace("Z", "+00:00"))
        local_tz = package_as_of.tzinfo
    except (TypeError, ValueError):
        local_tz = timezone.utc
    selected = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        published = _published_timestamp(row)
        if published is None or published > cutoff:
            continue
        published_day = published.astimezone(local_tz).date()
        if start and published_day < start:
            continue
        selected.append((published, row))
    selected.sort(key=lambda pair: pair[0], reverse=True)
    lines = [f"Source: shared India evidence package; publication timestamps and calendar known-at times are cutoff-filtered; cutoff={cutoff.isoformat()}."]
    for published, row in selected[:30]:
        lines.append(
            f"- NEWS published {published.isoformat()} | {row.get('title', 'Untitled')}\n"
            f"  Type={row.get('event_type', 'unknown')}; severity={row.get('severity', 'unknown')}; "
            f"direction={row.get('direction', 'uncertain')}\n"
            f"  Summary={row.get('summary', '')}\n  Source={row.get('source_url') or row.get('url') or 'missing'}"
        )
    calendar_selected = []
    for row in calendar_rows if isinstance(calendar_rows, list) else []:
        if not isinstance(row, dict):
            continue
        known_at = _parse_aware_timestamp(row.get("known_at_utc"))
        if known_at is None or known_at > cutoff:
            continue
        try:
            event_day = _as_date(str(row.get("event_date") or row.get("date") or ""))
        except (TypeError, ValueError):
            continue
        if start and event_day < start:
            continue
        calendar_selected.append((event_day, known_at, row))
    calendar_selected.sort(key=lambda item: item[0])
    for event_day, known_at, row in calendar_selected[:30]:
        lines.append(
            f"- SCHEDULED CALENDAR EVENT on {event_day.isoformat()} (known {known_at.isoformat()}) | {row.get('title', 'Untitled')}\n"
            f"  Category={row.get('category', 'unknown')}; verified={row.get('verified', False)}; "
            f"importance={row.get('importance', 'unknown')}; status={row.get('status', 'unknown')}\n"
            f"  Description={row.get('description', '')}\n  Source={row.get('source_url') or 'missing'}"
        )
    if not selected and not calendar_selected:
        return _not_available("get_news", "no precisely dated news or previously-known calendar rows are eligible for the requested window and analysis cutoff")
    research = package.get("web_research", {})
    if isinstance(research, dict):
        brief = research.get("brief")
        sources = research.get("sources", [])
        eligible_sources = []
        for source in sources if isinstance(sources, list) else []:
            if not isinstance(source, dict):
                continue
            published = _published_timestamp(source)
            if published is None or published > cutoff:
                continue
            published_day = published.astimezone(local_tz).date()
            if start and published_day < start:
                continue
            eligible_sources.append((published, source))
        if brief and eligible_sources:
            lines.append("\nPublication-filtered web research brief (not a verified filing):")
            lines.append(str(brief)[:3500])
            lines.append("Eligible research citations:")
            for published, source in eligible_sources[:10]:
                lines.append(f"- {published.isoformat()} | {source.get('title') or 'Untitled'} | {source.get('url')}")
    return "\n".join(lines)


def india_tool_result(tool_name: str, *args: Any, **kwargs: Any) -> str | None:
    """Return an India-specific tool result, or None when no scoped package is active."""
    package = active_package()
    if package is None:
        return None

    market = package.get("market_data", {}) if isinstance(package.get("market_data"), dict) else {}
    technical = market.get("technical_snapshot", {}) if isinstance(market.get("technical_snapshot"), dict) else {}
    facts = package.get("financial_facts", []) if isinstance(package.get("financial_facts"), list) else []
    ticker = str(package.get("symbol") or "unknown")

    if tool_name == "get_stock_data":
        # Pull the exact raw candles from the same provider family as the dashboard.
        try:
            start = _as_date(args[1])
            end = min(_as_date(args[2]), _as_date(package.get("as_of", args[2])))
            if end < start:
                return _not_available(tool_name, "requested range is after the analysis cutoff")
            from tradingagents.india.providers.fyers import FyersMarketDataClient
            from tradingagents.india.contracts import SwingTimeframe
            client = FyersMarketDataClient.from_env()
            candles = client.get_history(ticker, start, end, SwingTimeframe.DAILY)
            if not candles:
                return _not_available(tool_name, "FYERS returned no daily candles")
            cutoff_text = str(package.get("as_of", ""))
            cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
            if cutoff.tzinfo is None or cutoff.utcoffset() is None:
                return _not_available(tool_name, "evidence package cutoff has no timezone")
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["date", "open", "high", "low", "close", "volume", "source"])
            for candle in candles:
                close_time = getattr(candle, "close_time", None)
                is_complete = bool(getattr(candle, "is_complete", False))
                if close_time is None or close_time > cutoff or not is_complete:
                    continue
                writer.writerow([candle.open_time.isoformat(), candle.open, candle.high, candle.low, candle.close, candle.volume, "FYERS"])
            body = output.getvalue().strip()
            return body if body.count("\n") >= 1 else _not_available(tool_name, "no complete candles remained after cutoff filtering")
        except Exception as exc:
            return _not_available(tool_name, f"FYERS candle retrieval failed ({type(exc).__name__})")

    if tool_name in {"get_indicators", "get_verified_market_snapshot"}:
        timeframes = technical.get("timeframes", {}) if isinstance(technical.get("timeframes"), dict) else {}
        if not timeframes:
            return _not_available(tool_name, "technical snapshot contains no timeframe reports")
        requested = str(args[1]).lower() if tool_name == "get_indicators" and len(args) > 1 else "all"
        requested_names = [x.strip() for x in requested.split(",") if x.strip()]
        lines = [f"Source: shared India technical snapshot; provider={market.get('provider', 'unknown')}; as_of={package.get('as_of')}"]
        found_keys: set[str] = set()
        for tf, row in timeframes.items():
            if not isinstance(row, dict):
                continue
            lines.append(f"[{tf}] status={row.get('status', 'unknown')} candles={row.get('count', 'unknown')}")
            aliases = {"rsi": "rsi14", "atr": "atr14", "vwap": "session_vwap", "ema_20": "ema20", "ema_50": "ema50", "ema_100": "ema100", "ema_200": "ema200"}
            if requested != "all" and requested_names:
                keys = []
                for name in requested_names:
                    keys.append(aliases.get(name.replace(" ", "_"), name.replace(" ", "")))
                keys += ["status", "count", "latest_close", "trend", "support_20", "resistance_20"]
            else:
                keys = list(row.keys())
            for key in dict.fromkeys(keys):
                if key in row:
                    found_keys.add(key)
                    lines.append(f"  {key}={row[key]}")
        if requested != "all":
            missing = [name for name in requested_names if aliases.get(name.replace(" ", "_"), name.replace(" ", "")) not in found_keys]
            if missing:
                lines.append("Requested indicators absent from the shared snapshot (not substituted): " + ", ".join(missing))
        if tool_name == "get_verified_market_snapshot":
            lines.append("Verification note: this is the precomputed India technical snapshot, not a fresh independent provider verification.")
        return "\n".join(lines)

    if tool_name in {"get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement", "get_insider_transactions"}:
        if tool_name == "get_insider_transactions":
            return _not_available(tool_name, "no point-in-time Indian insider-transaction adapter is configured")
        cutoff = _package_cutoff(package)
        if cutoff is None:
            return _not_available(tool_name, "evidence package has no timezone-aware analysis cutoff")
        statements = package.get("financial_statements")
        section = {"get_income_statement": "income", "get_balance_sheet": "balance", "get_cashflow": "cashflow"}.get(tool_name)
        if section and isinstance(statements, dict):
            # Statement facts were already filtered to published_at <= as_of when the package was built.
            from tradingagents.india.fundamentals_view import statement_text

            freq = args[1] if len(args) > 1 and isinstance(args[1], str) else "annual"
            text = statement_text(statements, section, freq, symbol=ticker, as_of=package.get("as_of"))
            if text is not None:
                return text
            return _not_available(tool_name, f"no eligible {freq} {section} statement data in the shared evidence")
        eligible_facts = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            published = _published_timestamp(fact)
            if published is not None and published <= cutoff:
                eligible_facts.append(fact)
        if not eligible_facts:
            return _not_available(tool_name, "no precisely dated financial facts are eligible at the analysis cutoff")
        name_terms = {
            "get_balance_sheet": ("asset", "liabilit", "equity", "cash", "debt"),
            "get_cashflow": ("cashflow", "cash_flow", "operating_cash", "investing_cash", "financing_cash"),
            "get_income_statement": ("revenue", "profit", "income", "expense", "earning", "nii", "interest"),
        }.get(tool_name)
        selected = []
        for fact in eligible_facts:
            metric = str(fact.get("metric_code", "")).lower()
            if name_terms and not any(term in metric for term in name_terms):
                continue
            selected.append(fact)
        if not selected:
            return _not_available(tool_name, "no eligible facts match this statement category")
        lines = [f"Source: shared India financial-fact store; symbol={ticker}; as_of={package.get('as_of')}. These facts are not a complete normalized statement."]
        for fact in selected[:60]:
            lines.append(
                f"- {fact.get('metric_code')}: value={fact.get('value')} {fact.get('unit', '')} "
                f"currency={fact.get('currency') or 'unspecified'}; period={fact.get('period_start')}..{fact.get('period_end')} "
                f"({fact.get('period_type')}, scope={fact.get('scope')}); published={fact.get('published_at_utc') or fact.get('published_at')}; "
                f"source={fact.get('source_url') or fact.get('source_name') or 'unknown'}"
            )
        return "\n".join(lines)

    if tool_name == "get_news":
        return _events_text(package, start_date=args[1] if len(args) > 1 else None, end_date=args[2] if len(args) > 2 else None)

    return None

"""Peer valuation summaries from stored Yahoo retrieval-time snapshots.

Only recent snapshots participate in peer medians. These medians are calculated
from the stored Nifty 500 universe; they are not official NSE industry valuations.
Negative, missing, implausible, and stale P/E observations are excluded.
"""
from __future__ import annotations
import csv
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_UNIVERSE = Path(__file__).resolve().parent / "data" / "nifty_500_constituents.csv"
DEFAULT_MAX_SNAPSHOT_AGE_DAYS = 30


def _latest_metric(rows: list[dict[str, Any]], symbol: str, metric: str) -> dict[str, Any] | None:
    eligible = [r for r in rows if str(r.get("symbol", "")).upper() == symbol.upper()
                and r.get("metric_code") == metric and r.get("period_type") == "snapshot"]
    eligible.sort(key=lambda r: (str(r.get("retrieved_at", "")), str(r.get("period_end", ""))), reverse=True)
    return eligible[0] if eligible else None


def _snapshot_datetime(row: dict[str, Any] | None) -> datetime | None:
    if not row:
        return None
    raw = row.get("retrieved_at") or row.get("published_at")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _is_fresh(row: dict[str, Any] | None, *, now: datetime, max_age_days: float) -> bool:
    stamp = _snapshot_datetime(row)
    if stamp is None:
        return False
    age = now - stamp
    return timedelta(0) <= age <= timedelta(days=max_age_days)


def _positive_pe(row: dict[str, Any] | None) -> float | None:
    try:
        value = float(row["value"]) if row else None
        return value if value is not None and 0 < value < 10000 else None
    except (ValueError, TypeError, KeyError):
        return None


def _latest_snapshot(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    snapshots = [r for r in rows if str(r.get("symbol", "")).upper() == symbol.upper()
                 and r.get("period_type") == "snapshot"]
    return max(snapshots, key=lambda r: str(r.get("retrieved_at", "")), default=None)


def build_valuation_summary(
    symbol: str,
    rows: list[dict[str, Any]],
    universe_path: Path = DEFAULT_UNIVERSE,
    *,
    now: datetime | None = None,
    max_snapshot_age_days: float = DEFAULT_MAX_SNAPSHOT_AGE_DAYS,
) -> dict[str, Any]:
    """Build a peer comparison using recent snapshots only.

    ``max_snapshot_age_days`` is explicit and returned in the response so callers
    can explain why stored snapshots were excluded. ``now`` is injectable for tests.
    """
    symbol = symbol.upper()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if max_snapshot_age_days <= 0:
        raise ValueError("max_snapshot_age_days must be positive")

    own_snapshot = _latest_snapshot(rows, symbol)
    own_fresh = _is_fresh(own_snapshot, now=now, max_age_days=max_snapshot_age_days)
    own_pe_row = _latest_metric(rows, symbol, "PE_RATIO") if own_fresh else None
    forward_row = _latest_metric(rows, symbol, "FORWARD_PE") if own_fresh else None
    own_pe = _positive_pe(own_pe_row)
    industry = (own_snapshot or {}).get("yahoo_industry")
    sector = (own_snapshot or {}).get("yahoo_sector")

    universe: dict[str, dict[str, str]] = {}
    try:
        with universe_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("symbol"):
                    universe[row["symbol"].upper()] = row
    except OSError:
        pass
    if not industry and symbol in universe:
        sector = sector or universe[symbol].get("sector")

    snapshot_by_symbol: dict[str, dict[str, Any] | None] = {
        peer: _latest_snapshot(rows, peer) for peer in universe
    }
    eligible_pe: dict[str, float] = {}
    stale_or_missing_pe = 0
    for peer in universe:
        snap = snapshot_by_symbol[peer]
        metric = _latest_metric(rows, peer, "PE_RATIO")
        if not _is_fresh(snap, now=now, max_age_days=max_snapshot_age_days) or metric is None:
            stale_or_missing_pe += 1
            continue
        value = _positive_pe(metric)
        if value is not None:
            eligible_pe[peer] = value

    candidates: dict[str, float] = {}
    peer_group = "industry"
    if industry:
        for peer, value in eligible_pe.items():
            if peer == symbol:
                continue
            peer_industry = (snapshot_by_symbol[peer] or {}).get("yahoo_industry")
            if peer_industry and peer_industry.casefold() == industry.casefold():
                candidates[peer] = value
    if len(candidates) < 5:
        peer_group = "sector"
        candidates = {}
        if sector:
            for peer, value in eligible_pe.items():
                if peer == symbol:
                    continue
                snap = snapshot_by_symbol[peer] or {}
                peer_sector = snap.get("yahoo_sector") or universe.get(peer, {}).get("sector")
                if peer_sector and peer_sector.casefold() == sector.casefold():
                    candidates[peer] = value

    median = statistics.median(candidates.values()) if len(candidates) >= 5 else None
    nifty_values = list(eligible_pe.values())
    nifty_median = statistics.median(nifty_values) if len(nifty_values) >= 5 else None
    comparison = round((own_pe / median - 1) * 100, 1) if own_pe is not None and median is not None else None
    own_metric_for_stamp = own_pe_row or forward_row or own_snapshot or {}
    industry_count = sum(
        1 for peer, value in eligible_pe.items()
        if peer != symbol and industry and (snapshot_by_symbol[peer] or {}).get("yahoo_industry")
        and (snapshot_by_symbol[peer] or {}).get("yahoo_industry", "").casefold() == industry.casefold()
    )
    sector_count = sum(
        1 for peer, value in eligible_pe.items()
        if peer != symbol and sector and (((snapshot_by_symbol[peer] or {}).get("yahoo_sector") or universe.get(peer, {}).get("sector") or "").casefold() == sector.casefold())
    )
    return {
        "symbol": symbol,
        "stock": {"trailing_pe": own_pe, "forward_pe": _positive_pe(forward_row),
                  "pb": _metric_value(rows, symbol, "PB_RATIO", fresh=own_fresh),
                  "roe": _metric_value(rows, symbol, "ROE", fresh=own_fresh),
                  "fetched_at": own_metric_for_stamp.get("retrieved_at"), "snapshot_fresh": own_fresh,
                  "source": "Yahoo Finance (yfinance)"},
        "industry": industry, "sector": sector, "peer_group": peer_group,
        "peer_median": round(median, 2) if median is not None else None,
        "peer_count": len(candidates), "peer_low": round(min(candidates.values()), 2) if candidates else None,
        "peer_high": round(max(candidates.values()), 2) if candidates else None,
        "industry_peers_found": industry_count, "sector_peers_found": sector_count,
        "nifty500_median": round(nifty_median, 2) if nifty_median is not None else None,
        "eligible_nifty500_pe_count": len(nifty_values), "stale_or_missing_pe_count": stale_or_missing_pe,
        "max_snapshot_age_days": max_snapshot_age_days,
        "percent_vs_peers": comparison,
        "status": "ok" if median is not None else "not_enough_peer_data",
        "note": f"Peer medians use snapshots no older than {max_snapshot_age_days:g} days. Negative, missing, implausible and stale P/E observations are excluded. This is a calculated peer median, not an official NSE industry P/E; Yahoo P/E may mix standalone and consolidated earnings, and the median is not market-cap weighted.",
    }


def _metric_value(rows: list[dict[str, Any]], symbol: str, metric: str, *, fresh: bool = True) -> float | None:
    row = _latest_metric(rows, symbol, metric) if fresh else None
    try:
        return float(row["value"]) if row else None
    except (ValueError, TypeError, KeyError):
        return None

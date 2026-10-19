"""Persistent, compact snapshots of completed stock research runs.

Stores conclusions and selected evidence summaries, not raw provider payloads or secrets.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _db_path() -> Path:
    configured = os.getenv("TRADINGAGENTS_DB_PATH")
    return Path(configured) if configured else Path(__file__).resolve().parents[2] / "data" / "tradingagents_india.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS research_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL UNIQUE,
        symbol TEXT NOT NULL,
        as_of TEXT,
        saved_at TEXT NOT NULL,
        rating TEXT,
        conclusion TEXT,
        technical_bias TEXT,
        summary TEXT,
        snapshot_json TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_research_memory_symbol_saved ON research_memory(symbol, saved_at DESC)")
    return conn


def _first(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for key in keys:
        value = d.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _compact_text(value: Any, limit: int = 220) -> str | None:
    if value in (None, ""):
        return None
    return " ".join(str(value).split())[:limit]


def _event_signature(row: dict[str, Any], kind: str) -> dict[str, Any]:
    title = _first(row, "title", "headline", "event", "summary", "name")
    date = _first(row, "event_date", "date", "published_at", "published_at_utc")
    url = _first(row, "url", "link", "source_url")
    symbol = _first(row, "symbol", "ticker")
    source = _first(row, "source", "source_name", "provider", "domain")
    # Stable signature supports deduplication across reruns without asserting that
    # two similarly named records are the same underlying event with certainty.
    signature = "|".join(str(part or "").strip().casefold() for part in (kind, symbol, title, date, url))
    return {"kind": kind, "signature": signature[:700], "title": _compact_text(title),
            "date": _compact_text(date, 60), "url": _compact_text(url, 500),
            "source": _compact_text(source, 100), "known_at": _compact_text(_first(row, "known_at_utc", "created_at", "published_at"), 60)}


def _technical_metrics(value: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Flatten selected numeric indicator fields into timeframe.metric keys."""
    if depth > 5 or not isinstance(value, (dict, list)):
        return {}
    result: dict[str, Any] = {}
    wanted = ("close", "last_close", "ema20", "ema_20", "ema50", "ema_50", "ema100", "ema_100",
              "ema200", "ema_200", "rsi", "macd", "macd_signal", "adx", "atr", "vwap", "change_pct",
              "trend_score", "support", "resistance", "volume")
    items = value.items() if isinstance(value, dict) else enumerate(value[:8])
    for key, child in items:
        key_text = str(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        normalized = key_text.casefold().replace("-", "_")
        if isinstance(child, (int, float)) and not isinstance(child, bool) and any(token == normalized or normalized.startswith(token + "_") for token in wanted):
            result[path] = child
        elif isinstance(child, (dict, list)):
            result.update(_technical_metrics(child, path, depth + 1))
    return dict(list(result.items())[:80])


def _normalize_event_set(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    return {str(row.get("signature")): row for row in rows if isinstance(row, dict) and row.get("signature")}


def build_snapshot(symbol: str, analysis: dict[str, Any]) -> dict[str, Any]:
    technical = analysis.get("technical") if isinstance(analysis.get("technical"), dict) else {}
    assessment = analysis.get("assessment") if isinstance(analysis.get("assessment"), dict) else {}
    workflow = analysis.get("agent_workflow") if isinstance(analysis.get("agent_workflow"), dict) else {}
    evidence = analysis.get("evidence_package") if isinstance(analysis.get("evidence_package"), dict) else {}
    warnings = analysis.get("data_quality_warnings") or (evidence.get("data_quality") or {}).get("warnings") or []
    multi = technical.get("multi_timeframe") or technical.get("timeframes") or {}
    # Bound each nested section to keep the persistent memory compact and predictable.
    if isinstance(multi, dict):
        multi = {str(k): v for k, v in list(multi.items())[:8]}
    elif isinstance(multi, list):
        multi = multi[:8]
    sources = evidence.get("web_research", {}).get("sources", []) if isinstance(evidence.get("web_research"), dict) else []
    source_summary = []
    if isinstance(sources, list):
        for item in sources[:12]:
            if isinstance(item, dict):
                source_summary.append({k: item.get(k) for k in ("title", "url", "published_at", "source") if item.get(k) is not None})
    # Keep compact, comparable signatures for evidence and catalysts. Do not store
    # full articles or financial statements in long-lived research memory.
    package_news = evidence.get("news_and_events", []) if isinstance(evidence, dict) else []
    package_calendar = evidence.get("calendar_events", []) if isinstance(evidence, dict) else []
    news_summary = [_event_signature(row, "news") for row in package_news[:30] if isinstance(row, dict)] if isinstance(package_news, list) else []
    catalyst_summary = [_event_signature(row, "calendar") for row in package_calendar[:50] if isinstance(row, dict)] if isinstance(package_calendar, list) else []
    technical_metrics = _technical_metrics(multi)
    saved_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "run_id": str(analysis.get("run_id") or ""),
        "symbol": symbol.upper(),
        "as_of": analysis.get("as_of") or analysis.get("analysis_as_of") or technical.get("as_of"),
        "saved_at": saved_at,
        "rating": _first(analysis, "final_agent_rating", "decision") or _first(assessment, "decision", "overall_bias"),
        "conclusion": _first(analysis, "final_conclusion", "summary") or _first(workflow.get("summaries", {}), "final_trade_decision"),
        "technical_bias": _first(assessment, "technical_bias") or _first(technical, "multi_timeframe_bias", "bias"),
        "summary": str(_first(analysis, "summary", "executive_summary") or "")[:1800],
        "assessment": {k: assessment.get(k) for k in ("technical_bias", "risk_flag_count", "event_count_used", "financial_facts_used", "decision") if k in assessment},
        "technical_summary": multi,
        "technical_metrics": technical_metrics,
        "market_context": analysis.get("market_context") if isinstance(analysis.get("market_context"), dict) else {},
        "news_evidence": news_summary,
        "catalyst_evidence": catalyst_summary,
        "source_summary": source_summary,
        "data_quality_warnings": list(warnings)[:30] if isinstance(warnings, list) else [str(warnings)[:500]],
        "workflow_status": workflow.get("status"),
        "evidence_package_version": analysis.get("evidence_package_version"),
    }
    # Prevent oversized/opaque data from making this long-lived table balloon.
    snapshot["market_context"] = {k: v for k, v in list(snapshot["market_context"].items())[:20]}
    return snapshot


def save_snapshot(symbol: str, analysis: dict[str, Any]) -> dict[str, Any] | None:
    run_id = str(analysis.get("run_id") or "").strip()
    if not run_id:
        return None
    snapshot = build_snapshot(symbol, analysis)
    encoded = json.dumps(snapshot, ensure_ascii=False, default=str, separators=(",", ":"))
    with _connect() as conn:
        conn.execute("""INSERT INTO research_memory(run_id,symbol,as_of,saved_at,rating,conclusion,technical_bias,summary,snapshot_json)
          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET symbol=excluded.symbol, as_of=excluded.as_of,
          saved_at=excluded.saved_at, rating=excluded.rating, conclusion=excluded.conclusion,
          technical_bias=excluded.technical_bias, summary=excluded.summary, snapshot_json=excluded.snapshot_json""",
          (run_id, snapshot["symbol"], snapshot.get("as_of"), snapshot["saved_at"], str(snapshot.get("rating") or "")[:100],
           str(snapshot.get("conclusion") or "")[:3000], str(snapshot.get("technical_bias") or "")[:100], snapshot.get("summary", "")[:1800], encoded))
    return snapshot


def list_snapshots(symbol: str, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 50))
    with _connect() as conn:
        rows = conn.execute("SELECT snapshot_json FROM research_memory WHERE symbol=? ORDER BY COALESCE(as_of,saved_at) DESC, id DESC LIMIT ?", (symbol.upper(), limit)).fetchall()
    results = []
    for row in rows:
        try:
            item = json.loads(row["snapshot_json"])
            # History endpoint returns a summary; detailed technical evidence remains server-side.
            results.append({k: item.get(k) for k in ("run_id", "symbol", "as_of", "saved_at", "rating", "conclusion", "technical_bias", "summary", "assessment", "technical_metrics", "news_evidence", "catalyst_evidence", "source_summary", "workflow_status", "data_quality_warnings")})
        except (TypeError, json.JSONDecodeError):
            continue
    return results



def get_snapshot(symbol: str, run_id: str) -> dict[str, Any] | None:
    """Return one saved snapshot for a symbol/run pair, including compact detail fields."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT snapshot_json FROM research_memory WHERE symbol=? AND run_id=? LIMIT 1",
            (symbol.upper(), run_id),
        ).fetchone()
    if not row:
        return None
    try:
        item = json.loads(row["snapshot_json"])
        # Snapshot content is intentionally compact; do not return raw provider payloads.
        return item if isinstance(item, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None

def compare_snapshots(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Compare saved research summaries, technical metrics, evidence, and catalysts.

    This is a deterministic change detector, not a causal explanation or trading signal.
    Missing values are reported as unavailable rather than interpreted as zero.
    """
    fields = ("rating", "technical_bias", "conclusion", "workflow_status")
    changes = []
    for field in fields:
        old, new = previous.get(field), current.get(field)
        if old != new and (old not in (None, "") or new not in (None, "")):
            changes.append({"field": field, "previous": old, "current": new, "changed": True})
    old_assessment = previous.get("assessment") if isinstance(previous.get("assessment"), dict) else {}
    new_assessment = current.get("assessment") if isinstance(current.get("assessment"), dict) else {}
    for field in ("risk_flag_count", "event_count_used", "financial_facts_used"):
        old, new = old_assessment.get(field), new_assessment.get(field)
        if old != new and (old is not None or new is not None):
            changes.append({"field": f"assessment.{field}", "previous": old, "current": new, "changed": True})

    old_metrics = previous.get("technical_metrics") if isinstance(previous.get("technical_metrics"), dict) else {}
    new_metrics = current.get("technical_metrics") if isinstance(current.get("technical_metrics"), dict) else {}
    technical_changes = []
    for key in sorted(set(old_metrics) | set(new_metrics)):
        old, new = old_metrics.get(key), new_metrics.get(key)
        if old is None or new is None:
            technical_changes.append({"metric": key, "previous": old, "current": new, "change_type": "new_metric" if old is None else "metric_unavailable_now"})
        elif old != new:
            delta = round(float(new) - float(old), 8)
            pct = round((delta / abs(float(old))) * 100, 4) if float(old) != 0 else None
            technical_changes.append({"metric": key, "previous": old, "current": new, "absolute_change": delta, "percent_change": pct, "change_type": "value_changed"})
    technical_changes = technical_changes[:80]

    evidence_deltas: dict[str, Any] = {}
    for key in ("news_evidence", "source_summary"):
        old_rows = previous.get(key) if isinstance(previous.get(key), list) else []
        new_rows = current.get(key) if isinstance(current.get(key), list) else []
        def sig(row: dict[str, Any]) -> str:
            return str(row.get("signature") or row.get("url") or row.get("title") or json.dumps(row, sort_keys=True, default=str))
        old_map = {sig(row): row for row in old_rows if isinstance(row, dict)}
        new_map = {sig(row): row for row in new_rows if isinstance(row, dict)}
        evidence_deltas[key] = {"added": [new_map[k] for k in sorted(set(new_map) - set(old_map))][:20],
                                "no_longer_present": [old_map[k] for k in sorted(set(old_map) - set(new_map))][:20],
                                "previous_count": len(old_map), "current_count": len(new_map)}

    old_catalysts = _normalize_event_set(previous.get("catalyst_evidence"))
    new_catalysts = _normalize_event_set(current.get("catalyst_evidence"))
    added_catalysts = [new_catalysts[k] for k in sorted(set(new_catalysts) - set(old_catalysts))][:30]
    removed_catalysts = [old_catalysts[k] for k in sorted(set(old_catalysts) - set(new_catalysts))][:30]
    # Event-date status is only derived when both analysis dates and event date are
    # parseable. It describes schedule position, not event completion or verification.
    catalyst_status_changes = []
    def day(value: Any):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            try:
                return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return None
    previous_day, current_day = day(previous.get("as_of")), day(current.get("as_of"))
    if previous_day and current_day:
        for signature in sorted(set(old_catalysts) & set(new_catalysts)):
            event_day = day(new_catalysts[signature].get("date"))
            if not event_day:
                continue
            old_state = "upcoming" if event_day >= previous_day else "past"
            new_state = "upcoming" if event_day >= current_day else "past"
            if old_state != new_state:
                catalyst_status_changes.append({"event": new_catalysts[signature], "previous_state": old_state, "current_state": new_state,
                                                "note": "Date position changed relative to the analysis cutoff; actual occurrence is not verified."})

    old_warnings = set(map(str, previous.get("data_quality_warnings") or []))
    new_warnings = set(map(str, current.get("data_quality_warnings") or []))
    added_warnings = sorted(new_warnings - old_warnings)[:20]
    resolved_warnings = sorted(old_warnings - new_warnings)[:20]
    if not changes and not technical_changes and not any(v.get("added") or v.get("no_longer_present") for v in evidence_deltas.values()) and not added_catalysts and not removed_catalysts and not catalyst_status_changes and not added_warnings and not resolved_warnings:
        assessment = "no_material_change_detected_in_saved_summary"
    elif any(c["field"] in {"rating", "technical_bias"} for c in changes):
        assessment = "outlook_or_bias_changed"
    else:
        assessment = "research_evidence_or_metrics_changed"
    return {"previous_run_id": previous.get("run_id"), "current_run_id": current.get("run_id"),
            "previous_as_of": previous.get("as_of"), "current_as_of": current.get("as_of"),
            "assessment": assessment, "changes": changes, "technical_metric_changes": technical_changes,
            "evidence_deltas": evidence_deltas,
            "catalyst_changes": {"added": added_catalysts, "no_longer_present": removed_catalysts,
                                 "past_upcoming_transitions": catalyst_status_changes},
            "new_data_quality_warnings": added_warnings, "resolved_data_quality_warnings": resolved_warnings,
            "limitations": ["Only compact evidence signatures and selected numeric technical fields are compared; this is not a full candle-by-candle or article-text diff.",
                            "A catalyst moving from upcoming to past only reflects its date relative to the cutoff; occurrence and outcome are not verified.",
                            "Missing metrics or evidence records are not proof that the underlying condition changed.",
                            "A changed rating or indicator is not proof that the underlying investment thesis is correct or invalid."]}

"""Small local SQLite store for sourced events, financial facts and paper orders."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(os.getenv("TRADINGAGENTS_DB_PATH", "data/tradingagents_india.sqlite3"))


class IndiaStore:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
              event_id TEXT PRIMARY KEY, symbol TEXT, event_type TEXT NOT NULL,
              title TEXT NOT NULL, summary TEXT, source_name TEXT NOT NULL, source_url TEXT NOT NULL,
              published_at TEXT, retrieved_at TEXT NOT NULL, severity TEXT NOT NULL,
              direction TEXT NOT NULL, confidence REAL NOT NULL, payload_json TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS financial_facts (
              fact_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, metric_code TEXT NOT NULL,
              value REAL NOT NULL, unit TEXT NOT NULL, currency TEXT, period_start TEXT NOT NULL,
              period_end TEXT NOT NULL, period_type TEXT NOT NULL, scope TEXT NOT NULL,
              source_name TEXT NOT NULL, source_url TEXT NOT NULL, published_at TEXT NOT NULL,
              retrieved_at TEXT NOT NULL, filing_status TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_facts_symbol_period ON financial_facts(symbol, period_end);
            CREATE TABLE IF NOT EXISTS scan_jobs (
              job_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, params_json TEXT NOT NULL, result_json TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS calendar_events (
              event_id TEXT PRIMARY KEY, event_date TEXT NOT NULL, end_date TEXT,
              category TEXT NOT NULL, symbol TEXT, title TEXT NOT NULL,
              verified INTEGER NOT NULL DEFAULT 0, provider TEXT NOT NULL,
              created_at TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_calendar_date ON calendar_events(event_date, end_date);
            CREATE TABLE IF NOT EXISTS calendar_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS gcal_sync (event_id TEXT PRIMARY KEY, event_date TEXT NOT NULL, payload_hash TEXT NOT NULL, synced_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_orders (
              order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
              quantity INTEGER NOT NULL, reference_price REAL NOT NULL, stop_loss REAL,
              target REAL, status TEXT NOT NULL, created_at TEXT NOT NULL,
              filled_at TEXT NOT NULL, note TEXT
            );
            """)
            # Existing user databases predate the hidden flag; migrate in place.
            columns = {row[1] for row in c.execute("PRAGMA table_info(events)").fetchall()}
            if "hidden" not in columns:
                c.execute("ALTER TABLE events ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")

    def save_event(self, event: dict[str, Any]) -> None:
        # Keep a separate stored row for each affected symbol; a shared news URL
        # can be relevant to multiple companies/sectors. Payload retains the source event ID.
        symbol = (event.get("symbol") or "").strip().upper()
        storage_id = f"{event['event_id']}:{symbol or '_global'}"
        with self.connect() as c:
            c.execute("""INSERT OR IGNORE INTO events
                (event_id,symbol,event_type,title,summary,source_name,source_url,published_at,retrieved_at,severity,direction,confidence,payload_json,hidden)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)""", (
                storage_id, symbol or None, event["event_type"], event["title"],
                event.get("summary", ""), event["source_name"], event["source_url"],
                event.get("published_at"), event["retrieved_at"], event["severity"],
                event["direction"], float(event["confidence"]), json.dumps(event, ensure_ascii=False),
            ))

    def list_events(self, symbol: str | None = None, limit: int = 100, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        query = "SELECT payload_json, hidden FROM events"
        args: list[Any] = []
        clauses = []
        if symbol:
            clauses.append("symbol = ?")
            args.append(symbol.upper())
        if not include_hidden:
            clauses.append("hidden = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY retrieved_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self.connect() as c:
            rows = c.execute(query, args).fetchall()
            events = []
            for row in rows:
                item = json.loads(row[0])
                item["hidden"] = bool(row[1])
                events.append(item)
            return events

    def hide_event(self, event_id: str, *, hidden: bool = True) -> bool:
        """Hide/unhide a saved event without deleting its source record."""
        with self.connect() as c:
            row = c.execute("SELECT event_id FROM events WHERE event_id = ? OR event_id LIKE ? LIMIT 1",
                            (event_id, f"{event_id}:%")).fetchone()
            if row is None:
                return False
            c.execute("UPDATE events SET hidden = ? WHERE event_id = ? OR event_id LIKE ?",
                      (1 if hidden else 0, event_id, f"{event_id}:%"))
            return True

    def hide_events_below_score(self, minimum_score: int) -> int:
        """Hide low-quality rows in place; never delete saved events."""
        from tradingagents.india.news_quality import score_news_item
        with self.connect() as c:
            rows = c.execute("SELECT event_id,payload_json FROM events WHERE hidden = 0").fetchall()
            ids = []
            for row in rows:
                try:
                    item = score_news_item(json.loads(row[1]))
                    if item["relevance_score"] < minimum_score:
                        ids.append(row[0])
                except (ValueError, TypeError):
                    ids.append(row[0])
            if ids:
                c.executemany("UPDATE events SET hidden = 1 WHERE event_id = ?", [(x,) for x in ids])
            return len(ids)

    def save_fact(self, fact: dict[str, Any]) -> None:
        with self.connect() as c:
            c.execute("""INSERT OR REPLACE INTO financial_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                fact["fact_id"], fact["symbol"], fact["metric_code"], float(fact["value"]), fact["unit"],
                fact.get("currency"), fact["period_start"], fact["period_end"], fact["period_type"],
                fact["scope"], fact["source_name"], fact["source_url"], fact["published_at"],
                fact["retrieved_at"], fact["filing_status"], json.dumps(fact, ensure_ascii=False),
            ))

    def list_facts(self, symbol: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        query = "SELECT payload_json FROM financial_facts"
        args: list[Any] = []
        if symbol:
            query += " WHERE symbol = ?"
            args.append(symbol.upper())
        query += " ORDER BY period_end DESC, published_at DESC LIMIT ?"
        args.append(max(1, min(limit, 20000)))
        with self.connect() as c:
            return [json.loads(row[0]) for row in c.execute(query, args).fetchall()]

    def save_paper_order(self, order: dict[str, Any]) -> None:
        with self.connect() as c:
            c.execute("""INSERT INTO paper_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                order["order_id"], order["symbol"], order["side"], order["quantity"],
                order["reference_price"], order.get("stop_loss"), order.get("target"),
                order["status"], order["created_at"], order["filled_at"], order.get("note"),
            ))

    def list_paper_orders(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM paper_orders ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 5000)),)).fetchall()
            return [dict(row) for row in rows]

    def create_scan_job(self, job_id: str, params: dict[str, Any], now: str) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO scan_jobs(job_id,status,created_at,updated_at,params_json) VALUES (?,?,?,?,?)",
                      (job_id, "queued", now, now, json.dumps(params, ensure_ascii=False)))

    def update_scan_job(self, job_id: str, *, status: str, now: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        with self.connect() as c:
            c.execute("UPDATE scan_jobs SET status=?, updated_at=?, result_json=?, error=? WHERE job_id=?",
                      (status, now, json.dumps(result, ensure_ascii=False) if result is not None else None, error, job_id))

    def get_scan_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM scan_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["params"] = json.loads(item.pop("params_json"))
        item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
        return item

    def list_scan_jobs(self, limit: int = 20, *, completed_only: bool = False) -> list[dict[str, Any]]:
        """Return recent scan jobs, newest first, with decoded parameters/results."""
        query = "SELECT * FROM scan_jobs"
        if completed_only:
            query += " WHERE status IN ('completed','completed_with_errors') AND result_json IS NOT NULL"
        query += " ORDER BY updated_at DESC LIMIT ?"
        with self.connect() as c:
            rows = c.execute(query, (max(1, min(int(limit), 100)),)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item.pop("params_json"))
            item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
            items.append(item)
        return items

    # ---- Event calendar -------------------------------------------------
    def upsert_calendar_event(self, event: dict[str, Any]) -> bool:
        """Insert a calendar event; return True when a new row was created.

        An existing unverified row (for example detected from news) is upgraded
        in place when the same event arrives from an official/verified source.
        User-hidden rows stay hidden.
        """
        with self.connect() as c:
            row = c.execute("SELECT verified FROM calendar_events WHERE event_id = ?", (event["event_id"],)).fetchone()
            payload = json.dumps(event, ensure_ascii=False)
            if row is None:
                c.execute("""INSERT INTO calendar_events
                    (event_id,event_date,end_date,category,symbol,title,verified,provider,created_at,hidden,payload_json)
                    VALUES (?,?,?,?,?,?,?,?,?,0,?)""", (
                    event["event_id"], event["date"], event.get("end_date"), event["category"],
                    event.get("symbol"), event["title"], 1 if event.get("verified") else 0,
                    event["provider"], event["created_at"], payload))
                return True
            if event.get("verified") and not row["verified"]:
                c.execute("""UPDATE calendar_events SET event_date=?,end_date=?,category=?,symbol=?,title=?,
                    verified=1,provider=?,payload_json=? WHERE event_id=?""", (
                    event["date"], event.get("end_date"), event["category"], event.get("symbol"),
                    event["title"], event["provider"], payload, event["event_id"]))
            return False

    def list_calendar_events(self, start: str, end: str, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        """Events overlapping the inclusive ISO date window [start, end]."""
        query = "SELECT payload_json, hidden FROM calendar_events WHERE event_date <= ? AND COALESCE(end_date, event_date) >= ?"
        if not include_hidden:
            query += " AND hidden = 0"
        query += " ORDER BY event_date ASC, category ASC, title ASC LIMIT 5000"
        with self.connect() as c:
            rows = c.execute(query, (end, start)).fetchall()
        out = []
        for payload, hidden in rows:
            item = json.loads(payload)
            item["hidden"] = bool(hidden)
            out.append(item)
        return out

    def hide_calendar_event(self, event_id: str, *, hidden: bool = True) -> bool:
        with self.connect() as c:
            cur = c.execute("UPDATE calendar_events SET hidden = ? WHERE event_id = ?", (1 if hidden else 0, event_id))
            return cur.rowcount > 0

    def get_calendar_meta(self, key: str) -> str | None:
        with self.connect() as c:
            row = c.execute("SELECT value FROM calendar_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_calendar_meta(self, key: str, value: str) -> None:
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO calendar_meta(key,value) VALUES (?,?)", (key, value))

    # ---- Google Calendar sync bookkeeping ----------------------------------
    def gcal_sync_all(self) -> dict[str, dict[str, str]]:
        with self.connect() as c:
            rows = c.execute("SELECT event_id, event_date, payload_hash FROM gcal_sync").fetchall()
        return {r[0]: {"date": r[1], "hash": r[2]} for r in rows}

    def gcal_sync_set(self, event_id: str, event_date: str, payload_hash: str) -> None:
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO gcal_sync(event_id,event_date,payload_hash,synced_at) VALUES (?,?,?,datetime('now'))",
                      (event_id, event_date, payload_hash))

    def gcal_sync_delete(self, event_id: str) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM gcal_sync WHERE event_id = ?", (event_id,))

    def gcal_sync_clear(self) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM gcal_sync")

"""Durable storage and retrieval for research-only trading analyses.

Uses SQLite and JSON so it works with the project's existing local-storage setup.
It stores analytical outputs only; it never places or records broker orders.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now().astimezone().isoformat()


class AnalysisStore:
    """Persist full analysis snapshots and retrieve them by ID or symbol."""

    def __init__(self, path: str | Path = "data/trading_analysis.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        # SQLite disables foreign-key enforcement by default; enable it on every
        # connection so question records cannot reference nonexistent analyses.
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    analysis_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_analysis_symbol_asof
                    ON analysis_runs(symbol, as_of DESC);
                CREATE INDEX IF NOT EXISTS idx_analysis_created
                    ON analysis_runs(created_at DESC);
                CREATE TABLE IF NOT EXISTS analysis_questions (
                    question_id TEXT PRIMARY KEY,
                    analysis_id TEXT NOT NULL,
                    asked_at TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer_json TEXT NOT NULL,
                    FOREIGN KEY(analysis_id) REFERENCES analysis_runs(analysis_id)
                );
                CREATE INDEX IF NOT EXISTS idx_questions_analysis
                    ON analysis_questions(analysis_id, asked_at DESC);
                """
            )

    def save_analysis(self, state: dict[str, Any], *, analysis_id: str | None = None) -> str:
        """Save a complete state snapshot, returning its stable analysis ID."""
        request = state.get("request") or {}
        symbol = str(request.get("symbol") or "UNKNOWN").strip().upper()
        exchange = str(request.get("exchange") or "UNKNOWN").strip().upper()
        as_of = str(request.get("as_of") or "")
        if not as_of:
            raise ValueError("state.request.as_of is required for persistence")
        verdict_obj = state.get("final_verdict") or {"verdict": "NEEDS_REVIEW"}
        verdict = str(verdict_obj.get("verdict") or "NEEDS_REVIEW")
        identifier = analysis_id or str(state.get("run_id") or uuid4())
        created_at = _now()
        payload = dict(state)
        payload["analysis_id"] = identifier
        payload["persisted_at"] = created_at
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO analysis_runs
                (analysis_id, symbol, exchange, as_of, created_at, status, verdict, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(analysis_id) DO UPDATE SET
                    symbol=excluded.symbol, exchange=excluded.exchange,
                    as_of=excluded.as_of, status=excluded.status,
                    verdict=excluded.verdict, payload_json=excluded.payload_json""",
                (identifier, symbol, exchange, as_of, created_at,
                 str(state.get("status") or "unknown"), verdict, _json(payload)),
            )
        return identifier

    def get_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def list_analyses(self, symbol: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        query = "SELECT analysis_id, symbol, exchange, as_of, created_at, status, verdict FROM analysis_runs"
        args: list[Any] = []
        if symbol:
            query += " WHERE symbol = ?"
            args.append(symbol.strip().upper())
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, args).fetchall()]

    def save_question(self, analysis_id: str, question: str, answer: dict[str, Any]) -> str:
        if self.get_analysis(analysis_id) is None:
            raise KeyError(f"Unknown analysis_id: {analysis_id}")
        question_id = str(uuid4())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO analysis_questions VALUES (?, ?, ?, ?, ?)",
                (question_id, analysis_id, _now(), question, _json(answer)),
            )
        return question_id

    def list_questions(self, analysis_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT question_id, asked_at, question, answer_json FROM analysis_questions "
                "WHERE analysis_id = ? ORDER BY asked_at DESC LIMIT ?",
                (analysis_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [{**dict(row), "answer": json.loads(row["answer_json"])} for row in rows]

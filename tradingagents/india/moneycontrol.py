"""Permission-gated Moneycontrol statement-page fetcher.

Automated collection is disabled unless the operator has secured the required
written permission/licence. The adapter only accepts moneycontrol.com URLs and
returns extracted table rows with source provenance; it does not invent or
silently normalize financial facts.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import requests


class MoneycontrolPermissionError(PermissionError):
    pass


class MoneycontrolFetchError(RuntimeError):
    pass


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table = False
        self._row = False
        self._cell = False
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._cell_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table = True
            self._current_table = []
        elif self._table and tag == "tr":
            self._row = True
            self._current_row = []
        elif self._row and tag in {"td", "th"}:
            self._cell = True
            self._cell_text = []

    def handle_data(self, data: str) -> None:
        if self._cell:
            self._cell_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._cell and tag in {"td", "th"}:
            value = " ".join(part for part in self._cell_text if part).strip()
            self._current_row.append(value)
            self._cell = False
        elif self._row and tag == "tr":
            if any(self._current_row):
                self._current_table.append(self._current_row)
            self._row = False
        elif self._table and tag == "table":
            if self._current_table:
                self.tables.append(self._current_table)
            self._table = False


def fetch_statement_tables(url: str, *, timeout: float = 15.0, session: Any | None = None) -> dict[str, Any]:
    if os.getenv("MONEYCONTROL_PERMISSION_CONFIRMED", "false").lower() not in {"1", "true", "yes"}:
        raise MoneycontrolPermissionError(
            "Automated Moneycontrol collection is disabled. Obtain written permission/licensing, then set MONEYCONTROL_PERMISSION_CONFIRMED=true locally."
        )
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "moneycontrol.com" or host.endswith(".moneycontrol.com")):
        raise ValueError("Only HTTPS URLs on moneycontrol.com are accepted")
    client = session or requests.Session()
    try:
        response = client.get(url, timeout=timeout, headers={"User-Agent": "TradingAgents-India/0.2 (licensed financial research)"})
        response.raise_for_status()
    except requests.RequestException as exc:
        raise MoneycontrolFetchError(f"Moneycontrol request failed ({type(exc).__name__})") from exc
    parser = _TableParser()
    parser.feed(response.text)
    tables = [[row for row in table if row] for table in parser.tables]
    return {"source_name": "Moneycontrol", "source_url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "table_count": len(tables), "tables": tables,
            "note": "Extracted raw table rows only. Verify reporting period, units, standalone/consolidated basis and source publication time before importing numeric facts."}

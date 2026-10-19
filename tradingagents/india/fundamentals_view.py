"""Read-side helpers for stored Yahoo statement facts: dashboard tables, freshness, agent digest.

Pure functions over fact rows (dicts with metric_code, unit, period_type, period_end,
value, retrieved_at) so the same logic serves the dashboard, the evidence package
and the agent tools. No network calls.
"""
from __future__ import annotations

from typing import Any, Iterable

from tradingagents.india.statements import SOURCE_NAME, STATEMENT_METRICS
from tradingagents.india.storage import IndiaStore

SNAPSHOT_SOURCE = "Yahoo Finance (yfinance)"
CRORE = 1e7
SECTIONS = (("income", "Income statement", "income_stmt"),
            ("balance", "Balance sheet", "balance_sheet"),
            ("cashflow", "Cash flow", "cashflow"))
DEFAULT_PERIODS = {"annual": 4, "quarterly": 6}

STATEMENT_NOTE = (
    "Yahoo Finance provider statements, not official filings; standalone/consolidated basis is not stated by the "
    "provider. Money is INR crore, EPS is INR per share. For banks and lenders 'Revenue' is not total income: "
    "prefer Net Interest Income and Net Income."
)

_LABELS = {
    "REVENUE": "Revenue", "OPERATING_REVENUE": "Operating revenue", "GROSS_PROFIT": "Gross profit",
    "OPERATING_INCOME": "Operating income", "EBITDA": "EBITDA", "EBIT": "EBIT", "PRETAX_INCOME": "Pre-tax income",
    "TAX_PROVISION": "Tax provision", "NET_INCOME": "Net income", "NET_INCOME_COMMON": "Net income (common)",
    "EPS_BASIC": "EPS (basic)", "EPS_DILUTED": "EPS (diluted)", "INTEREST_INCOME": "Interest income",
    "INTEREST_EXPENSE": "Interest expense", "NET_INTEREST_INCOME": "Net interest income",
    "TOTAL_EXPENSES": "Total expenses", "TOTAL_ASSETS": "Total assets", "TOTAL_LIABILITIES": "Total liabilities",
    "SHAREHOLDERS_EQUITY": "Shareholders' equity", "TOTAL_DEBT": "Total debt", "NET_DEBT": "Net debt",
    "CASH_AND_EQUIVALENTS": "Cash & equivalents", "CURRENT_ASSETS": "Current assets",
    "CURRENT_LIABILITIES": "Current liabilities", "INVENTORY": "Inventory", "NET_PPE": "Net PP&E",
    "SHARES_OUTSTANDING": "Shares outstanding", "TOTAL_CAPITALIZATION": "Total capitalization",
    "OPERATING_CASH_FLOW": "Operating cash flow", "INVESTING_CASH_FLOW": "Investing cash flow",
    "FINANCING_CASH_FLOW": "Financing cash flow", "CAPEX": "Capital expenditure", "FREE_CASH_FLOW": "Free cash flow",
    "DIVIDENDS_PAID": "Dividends paid",
}

# metric_code -> (section key, display unit, divisor, order index)
_META: dict[str, tuple[str, str, float, int]] = {}
for _key, _title, _stmt in SECTIONS:
    for _i, (_code, _unit) in enumerate(STATEMENT_METRICS[_stmt].values()):
        if _unit == "INR":
            _META[_code] = (_key, "INR crore", CRORE, _i)
        elif _unit == "shares":
            _META[_code] = (_key, "crore shares", CRORE, _i)
        else:
            _META[_code] = (_key, "INR per share", 1.0, _i)


def is_statement_fact(row: dict[str, Any]) -> bool:
    return row.get("observation_type") == "provider_statement_snapshot" or (
        row.get("source_name") == SOURCE_NAME and row.get("metric_code") in _META)


def _round(metric: str, value: float) -> float:
    divisor = _META[metric][2]
    return round(value / divisor, 2 if divisor == 1.0 or divisor == CRORE else 2)


def build_statement_view(rows: Iterable[dict[str, Any]], period_type: str, max_periods: int | None = None) -> dict[str, Any]:
    """Metrics x periods table (newest period first) for one period type."""
    period_type = period_type.lower()
    limit = max_periods or DEFAULT_PERIODS.get(period_type, 4)
    picked: dict[tuple[str, str], tuple[str, float]] = {}  # (metric, period_end) -> (retrieved_at, value)
    retrieved: list[str] = []
    for row in rows:
        metric = row.get("metric_code")
        if metric not in _META or str(row.get("period_type", "")).lower() != period_type:
            continue
        try:
            value = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (metric, str(row["period_end"]))
        stamp = str(row.get("retrieved_at") or "")
        if stamp:
            retrieved.append(stamp)
        if key not in picked or stamp >= picked[key][0]:
            picked[key] = (stamp, value)
    periods = sorted({end for _, end in picked}, reverse=True)[:limit]
    keep = set(periods)
    sections: dict[str, list[dict[str, Any]]] = {k: [] for k, _, _ in SECTIONS}
    for metric in sorted({m for m, e in picked if e in keep}, key=lambda m: (_META[m][0], _META[m][3])):
        section, unit, _, _ = _META[metric]
        values = {end: _round(metric, picked[(metric, end)][1]) for end in periods if (metric, end) in picked}
        sections[section].append({"metric": metric, "label": _LABELS.get(metric, metric.title()),
                                  "unit": unit, "values": values})
    return {"period_type": period_type, "periods": periods, "sections": sections,
            "retrieved_at": max(retrieved) if retrieved else None, "note": STATEMENT_NOTE}


def statements_from_facts(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Compact annual+quarterly structure stored in the agent evidence package."""
    rows = [r for r in rows if is_statement_fact(r)]
    if not rows:
        return None
    out: dict[str, Any] = {"source_name": SOURCE_NAME, "note": STATEMENT_NOTE}
    for period_type in ("annual", "quarterly"):
        view = build_statement_view(rows, period_type)
        if view["periods"]:
            out[period_type] = {"periods": view["periods"], "sections": view["sections"]}
            out["retrieved_at"] = max(filter(None, [out.get("retrieved_at"), view["retrieved_at"]]), default=None)
    return out if ("annual" in out or "quarterly" in out) else None


def statement_text(statements: dict[str, Any], section: str, freq: str, *, symbol: str, as_of: Any) -> str | None:
    """Plain-text table for an agent tool; None when that frequency/section has no data."""
    freq = "quarterly" if str(freq).lower().startswith("q") else "annual"
    block = statements.get(freq)
    if not block or not block.get("sections", {}).get(section):
        return None
    title = {k: t for k, t, _ in SECTIONS}[section]
    lines = [f"Source: {statements.get('source_name', SOURCE_NAME)}, retrieved {statements.get('retrieved_at')}; "
             f"symbol={symbol}; as_of={as_of}; {freq} periods (newest first).",
             statements.get("note", STATEMENT_NOTE),
             f"{title.upper()} ({freq}); periods: " + " | ".join(block["periods"])]
    for item in block["sections"][section]:
        cells = ["n/a" if p not in item["values"] else str(item["values"][p]) for p in block["periods"]]
        lines.append(f"{item['label']} [{item['unit']}]: " + " | ".join(cells))
    return "\n".join(lines)


def _source_rows(store: IndiaStore, symbol: str, source_name: str) -> list[dict[str, Any]]:
    with store.connect() as conn:
        cur = conn.execute(
            "SELECT metric_code, unit, period_type, period_end, value, retrieved_at FROM financial_facts "
            "WHERE symbol = ? AND source_name = ?", (symbol.upper(), source_name))
        return [dict(r) for r in cur.fetchall()]


def statement_view(store: IndiaStore, symbol: str, period_type: str) -> dict[str, Any]:
    view = build_statement_view(_source_rows(store, symbol, SOURCE_NAME), period_type)
    view["symbol"] = symbol.upper()
    return view


def last_refreshed(store: IndiaStore, symbol: str) -> dict[str, str | None]:
    with store.connect() as conn:
        found = dict(conn.execute(
            "SELECT source_name, MAX(retrieved_at) FROM financial_facts WHERE symbol = ? GROUP BY source_name",
            (symbol.upper(),)).fetchall())
    return {"statements": found.get(SOURCE_NAME), "snapshot": found.get(SNAPSHOT_SOURCE)}

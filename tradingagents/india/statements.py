"""Yahoo Finance financial-statement ingestion for NSE equities.

Pulls annual and quarterly income statement, balance sheet and cash flow from
yfinance for ``SYMBOL.NS`` and stores them as sourced facts in the same
``financial_facts`` table used by CSV/XBRL imports.

Important limits (also written into every stored fact):

* These are *provider snapshots*, not official filings. Yahoo supplies no
  filing/publication date, so ``published_at`` is set to the retrieval time.
  The existing point-in-time guards therefore hide these facts for any
  ``as_of`` earlier than the retrieval time, which is the safe behaviour.
* Yahoo typically returns about four annual periods and about five quarterly
  periods. The coverage dict returned here reports what was actually found so
  gaps are visible instead of silently filled.
* Period start dates are approximate (end date minus a fixed span); only
  ``period_end`` is taken from the provider.
* Values are in the provider's reporting currency; INR is assumed for NSE
  symbols and recorded as ``currency_assumed``.
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from tradingagents.india.storage import IndiaStore

SOURCE_NAME = "Yahoo Finance (yfinance) statements"

# statement -> {yahoo row label: (metric_code, unit)}
# Banks report some lines under different labels; unknown labels are ignored,
# never guessed, and show up as missing in the coverage report.
STATEMENT_METRICS: dict[str, dict[str, tuple[str, str]]] = {
    "income_stmt": {
        "Total Revenue": ("REVENUE", "INR"),
        "Operating Revenue": ("OPERATING_REVENUE", "INR"),
        "Gross Profit": ("GROSS_PROFIT", "INR"),
        "Operating Income": ("OPERATING_INCOME", "INR"),
        "EBITDA": ("EBITDA", "INR"),
        "EBIT": ("EBIT", "INR"),
        "Pretax Income": ("PRETAX_INCOME", "INR"),
        "Tax Provision": ("TAX_PROVISION", "INR"),
        "Net Income": ("NET_INCOME", "INR"),
        "Net Income Common Stockholders": ("NET_INCOME_COMMON", "INR"),
        "Basic EPS": ("EPS_BASIC", "INR_per_share"),
        "Diluted EPS": ("EPS_DILUTED", "INR_per_share"),
        "Interest Income": ("INTEREST_INCOME", "INR"),
        "Interest Expense": ("INTEREST_EXPENSE", "INR"),
        "Net Interest Income": ("NET_INTEREST_INCOME", "INR"),
        "Total Expenses": ("TOTAL_EXPENSES", "INR"),
    },
    "balance_sheet": {
        "Total Assets": ("TOTAL_ASSETS", "INR"),
        "Total Liabilities Net Minority Interest": ("TOTAL_LIABILITIES", "INR"),
        "Stockholders Equity": ("SHAREHOLDERS_EQUITY", "INR"),
        "Total Debt": ("TOTAL_DEBT", "INR"),
        "Net Debt": ("NET_DEBT", "INR"),
        "Cash And Cash Equivalents": ("CASH_AND_EQUIVALENTS", "INR"),
        "Current Assets": ("CURRENT_ASSETS", "INR"),
        "Current Liabilities": ("CURRENT_LIABILITIES", "INR"),
        "Inventory": ("INVENTORY", "INR"),
        "Net PPE": ("NET_PPE", "INR"),
        "Ordinary Shares Number": ("SHARES_OUTSTANDING", "shares"),
        "Total Capitalization": ("TOTAL_CAPITALIZATION", "INR"),
    },
    "cashflow": {
        "Operating Cash Flow": ("OPERATING_CASH_FLOW", "INR"),
        "Investing Cash Flow": ("INVESTING_CASH_FLOW", "INR"),
        "Financing Cash Flow": ("FINANCING_CASH_FLOW", "INR"),
        "Capital Expenditure": ("CAPEX", "INR"),
        "Free Cash Flow": ("FREE_CASH_FLOW", "INR"),
        "Cash Dividends Paid": ("DIVIDENDS_PAID", "INR"),
    },
}

STATEMENT_ATTRS = {
    "income_stmt": ("income_stmt", "quarterly_income_stmt"),
    "balance_sheet": ("balance_sheet", "quarterly_balance_sheet"),
    "cashflow": ("cashflow", "quarterly_cashflow"),
}

# Days subtracted from period_end to approximate period_start.
_SPAN_DAYS = {"annual": 364, "quarterly": 89}


class StatementDataError(ValueError):
    pass


def _clean_symbol(symbol: str) -> str:
    normalized = (symbol or "").strip().upper()
    if not normalized or not normalized.replace("&", "").replace("-", "").replace("_", "").isalnum():
        raise StatementDataError("Invalid NSE symbol")
    return normalized


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_date(column: Any) -> date | None:
    try:
        if hasattr(column, "date"):
            return column.date()
        return date.fromisoformat(str(column)[:10])
    except (TypeError, ValueError):
        return None


def _frame_rows(frame: Any) -> list[tuple[str, date, float]]:
    """Flatten a yfinance statement DataFrame (rows=labels, columns=period ends)."""
    if frame is None or getattr(frame, "empty", True):
        return []
    out: list[tuple[str, date, float]] = []
    for label in frame.index:
        row = frame.loc[label]
        for column, raw in row.items():
            end, value = _to_date(column), _finite(raw)
            if end is not None and value is not None:
                out.append((str(label), end, value))
    return out


def refresh_yahoo_statements(
    symbol: str,
    store: IndiaStore,
    *,
    years: int = 5,
    ticker_factory: Callable[[str], Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Fetch and persist statement facts for one NSE symbol.

    Returns a coverage dict. Raises ``StatementDataError`` only when the
    provider call itself fails or returns nothing at all; partial data is stored
    and reported.
    """
    normalized = _clean_symbol(symbol)
    if ticker_factory is None:
        try:
            from tradingagents.dataflows.vendors.yahoo.india_statements import open_ticker
        except ImportError as exc:
            raise StatementDataError("yfinance is not installed in this environment.") from exc
        ticker_factory = open_ticker
    provider_symbol = f"{normalized}.NS"
    try:
        ticker = ticker_factory(provider_symbol)
    except Exception as exc:  # noqa: BLE001 - provider errors vary widely
        raise StatementDataError(f"Yahoo Finance request failed ({type(exc).__name__}); no facts were changed.") from exc

    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    cutoff = (today or now.date()) - timedelta(days=int(years * 366))
    source_url = f"https://finance.yahoo.com/quote/{provider_symbol}/financials/"

    coverage: dict[str, Any] = {}
    stored = 0
    fetch_errors = 0
    for statement, (annual_attr, quarterly_attr) in STATEMENT_ATTRS.items():
        mapping = STATEMENT_METRICS[statement]
        for period_type, attr in (("annual", annual_attr), ("quarterly", quarterly_attr)):
            key = f"{statement}.{period_type}"
            try:
                frame = getattr(ticker, attr)
            except Exception as exc:  # noqa: BLE001
                fetch_errors += 1
                coverage[key] = {"periods": 0, "metrics": 0, "earliest": None, "latest": None,
                                 "error": type(exc).__name__}
                continue
            periods: set[date] = set()
            metrics: set[str] = set()
            for label, end, value in _frame_rows(frame):
                if label not in mapping or end < cutoff:
                    continue
                metric_code, unit = mapping[label]
                start = end if statement == "balance_sheet" else end - timedelta(days=_SPAN_DAYS[period_type])
                fact = {
                    "symbol": normalized, "metric_code": metric_code, "value": value, "unit": unit,
                    "currency": "INR" if unit.startswith("INR") else None,
                    "period_start": start.isoformat(), "period_end": end.isoformat(),
                    "period_type": period_type, "scope": "unspecified",
                    "source_name": SOURCE_NAME, "source_url": source_url,
                    "published_at": stamp, "retrieved_at": stamp,
                    "filing_status": "unknown", "revision_of": None,
                    "observation_type": "provider_statement_snapshot",
                    "provider_symbol": provider_symbol, "provider_row_label": label,
                    "statement": statement, "currency_assumed": "INR",
                    "period_start_approximate": statement != "balance_sheet",
                    "note": ("Yahoo provider data, not an official filing. published_at is the retrieval time, "
                             "not the filing date; standalone/consolidated basis is not stated by the provider."),
                }
                fact["fact_id"] = hashlib.sha256(
                    "|".join((normalized, metric_code, end.isoformat(), period_type, SOURCE_NAME)).encode()
                ).hexdigest()[:32]
                store.save_fact(fact)
                stored += 1
                periods.add(end)
                metrics.add(metric_code)
            coverage[key] = {
                "periods": len(periods), "metrics": len(metrics),
                "earliest": min(periods).isoformat() if periods else None,
                "latest": max(periods).isoformat() if periods else None,
            }
    if stored == 0:
        reason = "all statement requests failed" if fetch_errors == len(coverage) else "Yahoo returned no statement data"
        raise StatementDataError(f"{reason} for {provider_symbol}; no facts were changed.")
    return {"symbol": normalized, "provider_symbol": provider_symbol, "source_name": SOURCE_NAME,
            "retrieved_at": stamp, "years_requested": years, "facts_stored": stored, "coverage": coverage,
            "note": "Provider snapshots, not filings. Check `coverage` for periods actually returned."}

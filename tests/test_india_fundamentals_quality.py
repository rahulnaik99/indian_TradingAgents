from datetime import date

import pandas as pd

from tradingagents.india.fundamentals_quality import analyse, spot_check
from tradingagents.india.statements import refresh_yahoo_statements
from tradingagents.india.storage import IndiaStore

TODAY = date(2026, 10, 10)


def _frame(rows, ends):
    return pd.DataFrame({pd.Timestamp(e): [r[1] for r in rows] for e in ends}, index=[r[0] for r in rows])


def _ticker(income_rows, balance_rows, ends=("2026-03-31", "2025-03-31"), qends=("2026-06-30",)):
    class T:
        def __init__(self, s): pass
        income_stmt = _frame(income_rows, ends)
        quarterly_income_stmt = _frame(income_rows, qends)
        balance_sheet = _frame(balance_rows, ends)
        quarterly_balance_sheet = pd.DataFrame()
        cashflow = pd.DataFrame()
        quarterly_cashflow = pd.DataFrame()
    return T


def test_quality_flags_and_spot_check(tmp_path):
    store = IndiaStore(tmp_path / "q.sqlite3")
    good = _ticker([("Total Revenue", 5e10), ("Net Income", 5e9), ("Diluted EPS", 12.5)],
                   [("Total Assets", 9e10), ("Stockholders Equity", 4e10)])
    bad = _ticker([("Total Revenue", 1e9), ("Net Income", 5e9)],
                  [("Total Assets", 1e9), ("Stockholders Equity", 4e9)])
    bank = _ticker([("Net Interest Income", 3e10), ("Net Income", 4e9)],
                   [("Total Assets", 9e11), ("Stockholders Equity", 8e10)])
    for sym, t in (("GOOD", good), ("BAD", bad), ("BANKY", bank)):
        refresh_yahoo_statements(sym, store, ticker_factory=t, today=TODAY)
    res = analyse(store, industries={"BANKY": "Financial Services"}, today=TODAY)
    by = {r["symbol"]: r for r in res["rows"]}
    assert by["GOOD"]["flags"] == ""
    assert "net_income_exceeds_revenue" in by["BAD"]["flags"]
    assert "equity_exceeds_assets" in by["BAD"]["flags"]
    assert by["BANKY"]["has_bank_lines"] and "net_income_exceeds_revenue" not in by["BANKY"]["flags"]
    assert by["GOOD"]["annual_periods"] == 2 and by["GOOD"]["quarterly_periods"] == 1
    assert res["key_metric_presence"]["financial"]["bank_lines (NII or interest income)"] == "1/1"

    spot = spot_check(store, ["good", "NOPE"])
    assert spot[0]["annual_INR_crore"]["REVENUE"]["2026-03-31"] == 5000.0  # 5e10 INR = 5000 crore
    assert spot[0]["annual_INR_crore"]["EPS_DILUTED"]["2026-03-31"] == 12.5
    assert spot[1]["error"] == "no stored facts"


def test_stale_symbol_flagged(tmp_path):
    store = IndiaStore(tmp_path / "q.sqlite3")
    old = _ticker([("Total Revenue", 1e9)], [("Total Assets", 2e9)], ends=("2025-03-31",), qends=("2025-06-30",))
    refresh_yahoo_statements("OLD", store, ticker_factory=old, today=date(2026, 10, 10))
    assert "stale_latest_annual" in analyse(store, today=date(2026, 10, 10))["rows"][0]["flags"]

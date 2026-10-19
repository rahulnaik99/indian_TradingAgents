from datetime import date

import pandas as pd
import pytest

from tradingagents.india.bulk_fundamentals import build_coverage, main, run_bulk
from tradingagents.india.statements import StatementDataError, refresh_yahoo_statements
from tradingagents.india.storage import IndiaStore
from tradingagents.india.universe import StockUniverseRegistry, UniverseRegistryError

TODAY = date(2026, 10, 10)


def _frame(labels, ends, base=100.0):
    data = {pd.Timestamp(e): [base + i for i, _ in enumerate(labels)] for e in ends}
    return pd.DataFrame(data, index=labels)


class FakeTicker:
    annual_ends = ["2026-03-31", "2025-03-31", "2024-03-31", "2023-03-31", "2019-03-31"]
    quarter_ends = ["2026-06-30", "2026-03-31"]

    def __init__(self, symbol):
        assert symbol.endswith(".NS")

    income_stmt = _frame(["Total Revenue", "Net Income", "Unmapped Row"], annual_ends)
    quarterly_income_stmt = _frame(["Total Revenue", "Net Income"], quarter_ends)
    balance_sheet = _frame(["Total Assets"], annual_ends[:3])
    quarterly_balance_sheet = pd.DataFrame()
    cashflow = _frame(["Operating Cash Flow"], annual_ends[:2])
    quarterly_cashflow = pd.DataFrame()


def test_statements_stored_with_provenance_and_year_cutoff(tmp_path):
    store = IndiaStore(tmp_path / "f.sqlite3")
    out = refresh_yahoo_statements("SBIN", store, years=5, ticker_factory=FakeTicker, today=TODAY)
    facts = store.list_facts("SBIN", limit=2000)
    assert out["facts_stored"] == len(facts) > 0
    assert all(f["observation_type"] == "provider_statement_snapshot" for f in facts)
    assert all(f["period_end"] >= "2021-01-01" for f in facts)  # 2019 period dropped by 5y cutoff
    assert not any(f["provider_row_label"] == "Unmapped Row" for f in facts)
    cov = out["coverage"]
    assert cov["income_stmt.annual"]["periods"] == 4
    assert cov["income_stmt.annual"]["earliest"] == "2023-03-31"
    assert cov["income_stmt.quarterly"]["periods"] == 2
    assert cov["balance_sheet.quarterly"]["periods"] == 0
    revenue = next(f for f in facts if f["metric_code"] == "REVENUE" and f["period_end"] == "2026-03-31"
                   and f["period_type"] == "annual")
    assert revenue["published_at"] == revenue["retrieved_at"]  # retrieval time, not a filing date


def test_rerun_overwrites_instead_of_duplicating(tmp_path):
    store = IndiaStore(tmp_path / "f.sqlite3")
    refresh_yahoo_statements("SBIN", store, ticker_factory=FakeTicker, today=TODAY)
    n1 = len(store.list_facts("SBIN", limit=2000))
    refresh_yahoo_statements("SBIN", store, ticker_factory=FakeTicker, today=TODAY)
    assert len(store.list_facts("SBIN", limit=2000)) == n1


def test_empty_provider_data_raises_and_stores_nothing(tmp_path):
    class Empty:
        def __init__(self, s): pass
        def __getattr__(self, name): return pd.DataFrame()
    store = IndiaStore(tmp_path / "f.sqlite3")
    with pytest.raises(StatementDataError):
        refresh_yahoo_statements("JUNK", store, ticker_factory=Empty)
    assert store.list_facts("JUNK") == []


def test_invalid_symbol_rejected(tmp_path):
    with pytest.raises(StatementDataError):
        refresh_yahoo_statements("bad symbol!", IndiaStore(tmp_path / "f.sqlite3"), ticker_factory=FakeTicker)


def test_bulk_resume_retry_and_circuit_breaker(tmp_path):
    store = IndiaStore(tmp_path / "f.sqlite3")
    progress = tmp_path / "p.jsonl"
    calls = []

    def fetcher(symbol, st, years=5):
        calls.append(symbol)
        if symbol == "BAD":
            raise StatementDataError("nothing")
        return {"facts_stored": 3, "retrieved_at": "2026-10-10T00:00:00+00:00",
                "coverage": {"income_stmt.annual": {"periods": 4, "earliest": "2023-03-31"}}}

    kw = dict(progress_path=progress, fetcher=fetcher, sleep=lambda s: None, log=lambda m: None, retries=2)
    r1 = run_bulk(["AAA", "BAD", "CCC"], store, **kw)
    assert (r1["done"], r1["failed"]) == (2, 1)
    assert calls.count("BAD") == 2  # retried

    calls.clear()
    r2 = run_bulk(["AAA", "BAD", "CCC"], store, **kw)
    assert r2["skipped_fresh"] == 2 and calls == ["BAD", "BAD"]  # only the failure is retried

    calls.clear()
    r3 = run_bulk(["B1", "BAD", "BAD2", "ZZZ"], store, max_consecutive_failures=2,
                  progress_path=tmp_path / "p2.jsonl",
                  fetcher=lambda s, st, years=5: (_ for _ in ()).throw(StatementDataError("blocked")),
                  sleep=lambda s: None, log=lambda m: None, retries=1)
    assert r3["halted"] and r3["failed"] == 2


def test_coverage_report(tmp_path):
    progress = tmp_path / "p.jsonl"
    store = IndiaStore(tmp_path / "f.sqlite3")
    run_bulk(["AAA"], store, progress_path=progress, sleep=lambda s: None, log=lambda m: None,
             fetcher=lambda s, st, years=5: {"facts_stored": 9, "retrieved_at": "x",
                                             "coverage": {"income_stmt.annual": {"periods": 4, "earliest": "2023-03-31"}}})
    rep = build_coverage(progress, years=5, out_csv=tmp_path / "cov.csv")
    assert rep["ok"] == 1 and rep["full_depth"] == 0 and rep["needs_filings_for_depth"] == 1
    assert rep["annual_income_periods_distribution"] == {4: 1}
    assert (tmp_path / "cov.csv").read_text().splitlines()[1].startswith("AAA,ok,9,4,2023-03-31")


NSE_CSV = ("Company Name,Industry,Symbol,Series,ISIN Code\n"
           "State Bank of India,Financial Services,SBIN,EQ,INE062A01020\n"
           "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n")


def test_nse_index_csv_loader_and_cli_import(tmp_path):
    src = tmp_path / "ind_nifty500list.csv"
    src.write_text(NSE_CSV)
    reg = StockUniverseRegistry.from_nse_index_csv(src, index_name="NIFTY 500", snapshot_date=date(2026, 9, 30))
    assert reg.symbols("NIFTY 500") == ("SBIN", "RELIANCE")
    out = tmp_path / "snap.csv"
    assert main(["universe-import", "--nse-csv", str(src), "--index", "NIFTY 500", "--date", "2026-09-30",
                 "--out", str(out)]) == 0
    assert StockUniverseRegistry.from_csv(out).symbols("NIFTY 500") == ("SBIN", "RELIANCE")
    assert main(["universe-import", "--nse-csv", str(src), "--index", "NIFTY 500", "--date", "2026-09-30",
                 "--out", str(out)]) == 2  # refuses to overwrite


def test_nse_index_csv_rejects_missing_columns(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Name,Ticker\nA,B\n")
    with pytest.raises(UniverseRegistryError):
        StockUniverseRegistry.from_nse_index_csv(bad, index_name="X", snapshot_date=date(2026, 1, 1))


def test_nse_index_csv_skips_dummy_placeholder_rows(tmp_path):
    src = tmp_path / "n.csv"
    src.write_text(NSE_CSV + "Dummy HEG Ltd.,Capital Goods,DUMMYHEG,EQ,DUM545A01024\n")
    reg = StockUniverseRegistry.from_nse_index_csv(src, index_name="NIFTY 500", snapshot_date=date(2026, 9, 30))
    assert "DUMMYHEG" not in reg.symbols("NIFTY 500")


def test_snapshot_failure_is_visible_and_retried_even_when_statements_are_fresh(tmp_path, monkeypatch):
    from tradingagents.india import fundamentals

    store = IndiaStore(tmp_path / "snapshot.sqlite3")
    progress = tmp_path / "progress.jsonl"
    statement_calls = []
    snapshot_calls = []

    def fetcher(symbol, st, years=5):
        statement_calls.append(symbol)
        return {"facts_stored": 2, "retrieved_at": "2026-10-11T00:00:00+00:00",
                "coverage": {"income_stmt.annual": {"periods": 5, "earliest": "2022-03-31"}}}

    def snapshot_fail(symbol, st):
        snapshot_calls.append(symbol)
        raise RuntimeError("provider throttled")

    monkeypatch.setattr(fundamentals, "refresh_yahoo_snapshot", snapshot_fail)
    kw = dict(progress_path=progress, fetcher=fetcher, with_snapshot=True, retries=1,
              sleep=lambda _s: None, log=lambda _m: None)
    first = run_bulk(["AAA"], store, **kw)
    assert first["done"] == 1
    assert _read_progress_for_test(progress)["AAA"]["snapshot_status"] == "failed"

    # A second run must retry the snapshot even though statement progress is fresh.
    run_bulk(["AAA"], store, **kw)
    assert statement_calls == ["AAA", "AAA"]
    assert snapshot_calls == ["AAA", "AAA"]

    report = build_coverage(progress, out_csv=tmp_path / "coverage.csv")
    assert report["snapshot_failed"] == 1
    assert "snapshot_error" in (tmp_path / "coverage.csv").read_text()


def _read_progress_for_test(path):
    import json
    return {r["symbol"]: r for r in (json.loads(line) for line in path.read_text().splitlines())}

"""Resumable bulk fundamentals job for a wide NSE universe, with a coverage report.

Run from the repository root::

    # 1. Convert an official NSE constituent CSV into a dated snapshot file
    python -m tradingagents.india.bulk_fundamentals universe-import \\
        --nse-csv ind_nifty500list.csv --index "NIFTY 500" --date 2026-09-30

    # 2. Fetch Yahoo statements for every member (resumable; Ctrl-C is safe)
    python -m tradingagents.india.bulk_fundamentals run \\
        --universe-csv tradingagents/india/data/nifty_500_constituents.csv --index "NIFTY 500"

    # 3. Re-build the coverage CSV / summary from progress at any time
    python -m tradingagents.india.bulk_fundamentals report

Design notes: one symbol at a time with a delay and jitter, retries with
backoff, a circuit breaker that stops after repeated consecutive failures (so a
block or rate limit is not hammered), and an append-only progress file so a
re-run skips symbols that are already done.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from tradingagents.india.statements import StatementDataError, refresh_yahoo_statements
from tradingagents.india.storage import IndiaStore
from tradingagents.india.universe import StockUniverseRegistry

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_PROGRESS = Path("data/fundamentals_bulk_progress.jsonl")
DEFAULT_COVERAGE = Path("data/fundamentals_coverage.csv")

COVERAGE_COLUMNS = [
    "symbol", "status", "facts_stored",
    "income_annual_periods", "income_annual_earliest", "income_quarterly_periods",
    "balance_annual_periods", "cashflow_annual_periods",
    "snapshot_status", "snapshot_metrics", "snapshot_retrieved_at", "snapshot_industry", "snapshot_sector", "snapshot_error",
    "needs_filings_for_depth", "retrieved_at", "error",
]


# ----------------------------------------------------------------- progress
def _read_progress(path: Path) -> dict[str, dict[str, Any]]:
    """Latest progress record per symbol (later lines win; bad lines are skipped)."""
    latest: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return latest
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
                latest[record["symbol"]] = record
            except (ValueError, KeyError, TypeError):
                continue
    return latest


def _append_progress(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _is_fresh(record: dict[str, Any] | None, max_age_days: float, now: datetime, *, require_snapshot: bool = False) -> bool:
    if not record or record.get("status") != "ok":
        return False
    # A statement refresh is not a complete valuation refresh. Legacy progress
    # rows and failed snapshot attempts must be retried when snapshots are required.
    if require_snapshot and record.get("snapshot_status") != "ok":
        return False
    try:
        done = datetime.fromisoformat(record["ts"])
    except (KeyError, ValueError):
        return False
    return now - done < timedelta(days=max_age_days)


# ---------------------------------------------------------------------- run
def run_bulk(
    symbols: Iterable[str],
    store: IndiaStore,
    *,
    progress_path: Path = DEFAULT_PROGRESS,
    years: int = 5,
    delay: float = 1.5,
    retries: int = 3,
    max_consecutive_failures: int = 15,
    refresh_after_days: float = 7.0,
    force: bool = False,
    with_snapshot: bool = False,
    fetcher: Callable[..., dict[str, Any]] = refresh_yahoo_statements,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    on_progress: Callable[[int, int, str, str], None] | None = None,
) -> dict[str, Any]:
    symbols = list(dict.fromkeys(s.strip().upper() for s in symbols if s and s.strip()))
    progress = _read_progress(progress_path)
    now = datetime.now(timezone.utc)
    done = skipped = failed = 0
    consecutive = 0
    halted = False
    for index, symbol in enumerate(symbols, start=1):
        if not force and _is_fresh(progress.get(symbol), refresh_after_days, now, require_snapshot=with_snapshot):
            skipped += 1
            if on_progress:
                on_progress(index, len(symbols), symbol, "skipped")
            continue
        result: dict[str, Any] | None = None
        error = ""
        for attempt in range(1, retries + 1):
            try:
                result = fetcher(symbol, store, years=years)
                break
            except StatementDataError as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - never let one symbol kill the batch
                error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                sleep(delay * (2 ** attempt) + random.uniform(0, 1))
        record: dict[str, Any] = {"symbol": symbol, "ts": datetime.now(timezone.utc).isoformat()}
        if result is not None:
            record.update(status="ok", facts_stored=result["facts_stored"], coverage=result["coverage"],
                          retrieved_at=result["retrieved_at"])
            if with_snapshot:
                try:
                    from tradingagents.india.fundamentals import refresh_yahoo_snapshot
                    snapshot = refresh_yahoo_snapshot(symbol, store)
                    record.update(snapshot_status="ok", snapshot_metrics=snapshot.get("inserted", 0),
                                  snapshot_retrieved_at=snapshot.get("retrieved_at"),
                                  snapshot_industry=snapshot.get("yahoo_industry"),
                                  snapshot_sector=snapshot.get("yahoo_sector"),
                                  snapshot_unavailable_metrics=snapshot.get("unavailable_metrics", []))
                except Exception as exc:  # noqa: BLE001 - statement data remains useful
                    record.update(snapshot_status="failed", snapshot_metrics=0,
                                  snapshot_error=f"{type(exc).__name__}: {str(exc)[:200]}")
            done += 1
            consecutive = 0
            log(f"[{index}/{len(symbols)}] {symbol}: ok ({result['facts_stored']} facts)")
        else:
            record.update(status="failed", error=error[:300])
            failed += 1
            consecutive += 1
            log(f"[{index}/{len(symbols)}] {symbol}: FAILED - {error[:120]}")
        _append_progress(progress_path, record)
        if on_progress:
            on_progress(index, len(symbols), symbol, record["status"])
        if consecutive >= max_consecutive_failures:
            halted = True
            log(f"Stopping: {consecutive} consecutive failures (possible block or rate limit). "
                "Wait a while and re-run; finished symbols will be skipped.")
            break
        sleep(delay + random.uniform(0, delay * 0.5))
    return {"requested": len(symbols), "done": done, "skipped_fresh": skipped, "failed": failed, "halted": halted}


# ------------------------------------------------------------------- report
def progress_summary(progress_path: Path, symbols: Iterable[str], *, max_age_days: float = 7.0) -> dict[str, Any]:
    """Universe-wide freshness from the progress file (for the dashboard)."""
    progress = _read_progress(progress_path)
    now = datetime.now(timezone.utc)
    symbols = list(symbols)
    stamps, fresh, failed = [], 0, []
    snapshot_ok = snapshot_failed = snapshot_missing = snapshot_fresh = 0
    industry_metadata = sector_metadata = 0
    for sym in symbols:
        rec = progress.get(sym)
        if rec is None:
            snapshot_missing += 1
            continue
        if rec.get("status") != "ok":
            failed.append(sym)
            snapshot_missing += 1
            continue
        stamps.append(rec["ts"])
        fresh += _is_fresh(rec, max_age_days, now)
        if rec.get("snapshot_status") == "ok":
            snapshot_ok += 1
            industry_metadata += bool(rec.get("snapshot_industry"))
            sector_metadata += bool(rec.get("snapshot_sector"))
            try:
                snap_at = datetime.fromisoformat(rec.get("snapshot_retrieved_at", ""))
                snapshot_fresh += (now - snap_at) < timedelta(days=max_age_days)
            except (ValueError, TypeError):
                pass
        elif rec.get("snapshot_status") == "failed":
            snapshot_failed += 1
        else:
            snapshot_missing += 1
    return {"universe_size": len(symbols), "refreshed_symbols": len(stamps), "fresh_within_days": fresh,
            "stale_or_missing": len(symbols) - fresh - len(failed), "failed": len(failed),
            "failed_symbols": failed[:30], "max_age_days": max_age_days,
            "oldest_refresh": min(stamps) if stamps else None, "newest_refresh": max(stamps) if stamps else None,
            "snapshot_ok": snapshot_ok, "snapshot_failed": snapshot_failed,
            "snapshot_missing": snapshot_missing, "snapshot_fresh_within_days": snapshot_fresh,
            "industry_metadata_available": industry_metadata, "sector_metadata_available": sector_metadata}


def build_coverage(progress_path: Path = DEFAULT_PROGRESS, *, years: int = 5,
                   out_csv: Path | None = DEFAULT_COVERAGE) -> dict[str, Any]:
    progress = _read_progress(progress_path)
    rows: list[dict[str, Any]] = []
    for symbol in sorted(progress):
        rec = progress[symbol]
        cov = rec.get("coverage") or {}
        income_a = cov.get("income_stmt.annual", {})
        row = {
            "symbol": symbol, "status": rec.get("status"), "facts_stored": rec.get("facts_stored", 0),
            "income_annual_periods": income_a.get("periods", 0),
            "income_annual_earliest": income_a.get("earliest"),
            "income_quarterly_periods": cov.get("income_stmt.quarterly", {}).get("periods", 0),
            "balance_annual_periods": cov.get("balance_sheet.annual", {}).get("periods", 0),
            "cashflow_annual_periods": cov.get("cashflow.annual", {}).get("periods", 0),
            "snapshot_status": rec.get("snapshot_status", "not_requested"),
            "snapshot_metrics": rec.get("snapshot_metrics", 0),
            "snapshot_retrieved_at": rec.get("snapshot_retrieved_at", ""),
            "snapshot_industry": rec.get("snapshot_industry", ""),
            "snapshot_sector": rec.get("snapshot_sector", ""),
            "snapshot_error": rec.get("snapshot_error", ""),
            "needs_filings_for_depth": rec.get("status") != "ok" or income_a.get("periods", 0) < years,
            "retrieved_at": rec.get("retrieved_at"), "error": rec.get("error", ""),
        }
        rows.append(row)
    if out_csv is not None:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=COVERAGE_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    ok = [r for r in rows if r["status"] == "ok"]
    distribution: dict[int, int] = {}
    for r in ok:
        distribution[r["income_annual_periods"]] = distribution.get(r["income_annual_periods"], 0) + 1
    return {
        "symbols_in_progress": len(rows), "ok": len(ok), "failed": len(rows) - len(ok),
        "annual_income_periods_distribution": dict(sorted(distribution.items())),
        "full_depth": sum(1 for r in ok if r["income_annual_periods"] >= years),
        "needs_filings_for_depth": sum(1 for r in rows if r["needs_filings_for_depth"]),
        "years_target": years, "csv": str(out_csv) if out_csv else None,
        "failed_symbols": [r["symbol"] for r in rows if r["status"] != "ok"][:50],
        "snapshot_ok": sum(1 for r in rows if r.get("snapshot_status") == "ok"),
        "snapshot_failed": sum(1 for r in rows if r.get("snapshot_status") == "failed"),
        "snapshot_missing": sum(1 for r in rows if r.get("snapshot_status") not in ("ok", "failed")),
        "industry_metadata_available": sum(1 for r in rows if r.get("snapshot_status") == "ok" and bool(r.get("snapshot_industry"))),
        "sector_metadata_available": sum(1 for r in rows if r.get("snapshot_status") == "ok" and bool(r.get("snapshot_sector"))),
    }


# ---------------------------------------------------------------------- CLI
def _slug(name: str) -> str:
    return "_".join(name.casefold().split())


def _resolve_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return [s for s in args.symbols.split(",") if s.strip()]
    if args.symbols_file:
        return [ln.strip() for ln in Path(args.symbols_file).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
    registry = (StockUniverseRegistry.from_csv(args.universe_csv) if args.universe_csv
                else StockUniverseRegistry.builtin())
    return list(registry.symbols(args.index))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("universe-import", help="convert an official NSE constituent CSV to a dated snapshot CSV")
    imp.add_argument("--nse-csv", required=True)
    imp.add_argument("--index", required=True, help='e.g. "NIFTY 500"')
    imp.add_argument("--date", required=True, help="effective date YYYY-MM-DD")
    imp.add_argument("--out", help="output CSV (default: data/<index>_constituents.csv; never overwrites)")

    run = sub.add_parser("run", help="fetch statements for a universe")
    run.add_argument("--universe-csv", help="snapshot CSV (default: built-in NIFTY 50)")
    run.add_argument("--index", default="NIFTY 50")
    run.add_argument("--symbols", help="comma-separated symbols (overrides universe)")
    run.add_argument("--symbols-file", help="one symbol per line (overrides universe)")
    run.add_argument("--limit", type=int, help="only the first N symbols (for a trial run)")
    run.add_argument("--years", type=int, default=5)
    run.add_argument("--delay", type=float, default=1.5, help="seconds between symbols (jittered)")
    run.add_argument("--retries", type=int, default=3)
    run.add_argument("--max-consecutive-failures", type=int, default=15)
    run.add_argument("--refresh-after-days", type=float, default=7.0)
    run.add_argument("--force", action="store_true", help="ignore progress and refetch everything")
    run.add_argument("--with-snapshot", action="store_true", help="also refresh the Yahoo P/E-style snapshot (extra request)")
    run.add_argument("--progress", default=str(DEFAULT_PROGRESS))
    run.add_argument("--coverage-csv", default=str(DEFAULT_COVERAGE))
    run.add_argument("--db", help="SQLite path (default: TRADINGAGENTS_DB_PATH or data/tradingagents_india.sqlite3)")

    rep = sub.add_parser("report", help="rebuild the coverage CSV and print a summary")
    rep.add_argument("--years", type=int, default=5)
    rep.add_argument("--progress", default=str(DEFAULT_PROGRESS))
    rep.add_argument("--coverage-csv", default=str(DEFAULT_COVERAGE))

    args = parser.parse_args(argv)

    if args.cmd == "universe-import":
        effective = date.fromisoformat(args.date)
        registry = StockUniverseRegistry.from_nse_index_csv(args.nse_csv, index_name=args.index, snapshot_date=effective)
        snap = registry.get_snapshot(args.index)
        out = Path(args.out) if args.out else DATA_DIR / f"{_slug(args.index)}_constituents.csv"
        if out.exists():
            print(f"Refusing to overwrite {out}; pass --out with a new name (snapshots are versioned).", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["index_name", "snapshot_date", "source_name", "source_url", "symbol",
                             "company_name", "exchange", "isin", "sector", "industry"])
            for ins in snap.instruments:
                writer.writerow([snap.index_name, snap.snapshot_date.isoformat(), snap.source_name, snap.source_url,
                                 ins.symbol, ins.company_name, ins.exchange, ins.isin or "", ins.sector or "",
                                 ins.industry or ""])
        print(f"Wrote {len(snap.instruments)} symbols to {out}")
        return 0

    if args.cmd == "report":
        print(json.dumps(build_coverage(Path(args.progress), years=args.years, out_csv=Path(args.coverage_csv)), indent=2))
        return 0

    symbols = _resolve_symbols(args)
    if args.limit:
        symbols = symbols[: args.limit]
    store = IndiaStore(args.db) if args.db else IndiaStore()
    print(f"Fetching statements for {len(symbols)} symbols (delay ~{args.delay}s each)...")
    try:
        summary = run_bulk(
            symbols, store, progress_path=Path(args.progress), years=args.years, delay=args.delay,
            retries=args.retries, max_consecutive_failures=args.max_consecutive_failures,
            refresh_after_days=args.refresh_after_days, force=args.force, with_snapshot=args.with_snapshot,
        )
    except KeyboardInterrupt:
        print("Interrupted; progress saved. Re-run to resume.")
        summary = {"interrupted": True}
    print(json.dumps(summary))
    print(json.dumps(build_coverage(Path(args.progress), years=args.years, out_csv=Path(args.coverage_csv)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

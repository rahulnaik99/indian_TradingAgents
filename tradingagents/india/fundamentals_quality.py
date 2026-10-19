"""Quality report for stored Yahoo statement facts: quarterly depth, missing lines, sanity flags, spot-check.

    python -m tradingagents.india.fundamentals_quality
    python -m tradingagents.india.fundamentals_quality --spot RBLBANK,SBIN,TCS,RELIANCE,ITC

Reads the facts already stored by ``bulk_fundamentals``; makes no network calls.
Writes ``data/fundamentals_quality.csv`` (one row per symbol) and prints a summary
plus the latest annual figures (in INR crore) for the spot-check symbols so you can
compare them by eye with each company's published results.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from tradingagents.india.bulk_fundamentals import DATA_DIR
from tradingagents.india.statements import SOURCE_NAME
from tradingagents.india.storage import IndiaStore

CRORE = 1e7
KEY_METRICS = ["REVENUE", "NET_INCOME", "EPS_DILUTED", "TOTAL_ASSETS", "SHAREHOLDERS_EQUITY", "OPERATING_CASH_FLOW"]
BANK_METRICS = ["NET_INTEREST_INCOME", "INTEREST_INCOME"]
SPOT_METRICS = ["REVENUE", "NET_INCOME", "EPS_DILUTED", "TOTAL_ASSETS", "SHAREHOLDERS_EQUITY",
                "OPERATING_CASH_FLOW", "NET_INTEREST_INCOME", "TOTAL_DEBT"]
QUALITY_COLUMNS = ["symbol", "group", "annual_periods", "quarterly_periods", "latest_annual", "latest_quarter",
                   "missing_key_metrics", "has_bank_lines", "flags"]


def _load(store: IndiaStore) -> dict[str, dict[tuple[str, str], dict[str, float]]]:
    """symbol -> (metric, period_type) -> {period_end: value}"""
    data: dict[str, dict[tuple[str, str], dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    with store.connect() as conn:
        for sym, metric, ptype, end, value in conn.execute(
            "SELECT symbol, metric_code, period_type, period_end, value FROM financial_facts WHERE source_name = ?",
            (SOURCE_NAME,),
        ):
            data[sym][(metric, ptype)][end] = value
    return data


def _industries(universe_csv: Path | None) -> dict[str, str]:
    if not universe_csv or not universe_csv.is_file():
        return {}
    with universe_csv.open(encoding="utf-8-sig", newline="") as stream:
        return {r["symbol"].strip().upper(): (r.get("industry") or "").strip() for r in csv.DictReader(stream)}


def analyse(store: IndiaStore, *, industries: dict[str, str] | None = None, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    industries = industries or {}
    data = _load(store)
    rows: list[dict[str, Any]] = []
    for sym in sorted(data):
        m = data[sym]
        group = "financial" if industries.get(sym) == "Financial Services" else "non-financial"

        def periods(metric: str, ptype: str) -> dict[str, float]:
            return m.get((metric, ptype), {})

        annual = sorted(set().union(*[periods(k, "annual").keys() for k in KEY_METRICS]))
        quarterly = sorted(set().union(*[periods(k, "quarterly").keys() for k in KEY_METRICS]))
        # a metric counts as present if it has any annual value
        missing = [k for k in KEY_METRICS if not periods(k, "annual")]
        has_bank = any(periods(k, "annual") for k in BANK_METRICS)
        flags: list[str] = []
        if annual and date.fromisoformat(annual[-1]) < today - timedelta(days=15 * 30):
            flags.append("stale_latest_annual")
        if not annual:
            flags.append("no_annual_data")
        rev, ni = periods("REVENUE", "annual"), periods("NET_INCOME", "annual")
        if group == "non-financial":
            if any(ni[e] > rev[e] > 0 for e in ni.keys() & rev.keys()):
                flags.append("net_income_exceeds_revenue")
        assets, eq = periods("TOTAL_ASSETS", "annual"), periods("SHAREHOLDERS_EQUITY", "annual")
        if any(v <= 0 for v in assets.values()):
            flags.append("nonpositive_assets")
        if any(eq[e] > assets[e] > 0 for e in eq.keys() & assets.keys()):
            flags.append("equity_exceeds_assets")
        if group == "financial" and not has_bank and "REVENUE" in missing:
            flags.append("financial_without_income_line")
        rows.append({"symbol": sym, "group": group, "annual_periods": len(annual), "quarterly_periods": len(quarterly),
                     "latest_annual": annual[-1] if annual else "", "latest_quarter": quarterly[-1] if quarterly else "",
                     "missing_key_metrics": ";".join(missing), "has_bank_lines": has_bank, "flags": ";".join(flags)})

    def dist(key: str) -> dict[int, int]:
        out: dict[int, int] = defaultdict(int)
        for r in rows:
            out[r[key]] += 1
        return dict(sorted(out.items()))

    presence: dict[str, dict[str, str]] = {}
    for grp in ("financial", "non-financial"):
        g = [r for r in rows if r["group"] == grp]
        if g:
            presence[grp] = {k: f"{sum(1 for r in g if k not in r['missing_key_metrics'].split(';'))}/{len(g)}"
                             for k in KEY_METRICS}
            if grp == "financial":
                presence[grp]["bank_lines (NII or interest income)"] = f"{sum(1 for r in g if r['has_bank_lines'])}/{len(g)}"
    flag_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        for f in filter(None, r["flags"].split(";")):
            flag_counts[f] += 1
    return {"symbols": len(rows), "annual_periods": dist("annual_periods"), "quarterly_periods": dist("quarterly_periods"),
            "key_metric_presence": presence, "flag_counts": dict(flag_counts),
            "flagged_symbols": {f: [r["symbol"] for r in rows if f in r["flags"].split(";")][:25] for f in flag_counts},
            "rows": rows}


def spot_check(store: IndiaStore, symbols: list[str]) -> list[dict[str, Any]]:
    data = _load(store)
    out = []
    for sym in symbols:
        m = data.get(sym.upper())
        if not m:
            out.append({"symbol": sym.upper(), "error": "no stored facts"})
            continue
        entry: dict[str, Any] = {"symbol": sym.upper(), "annual_INR_crore": {}}
        for metric in SPOT_METRICS:
            series = m.get((metric, "annual"))
            if not series:
                continue
            entry["annual_INR_crore"][metric] = {
                end: (round(val, 2) if metric == "EPS_DILUTED" else round(val / CRORE, 1))
                for end, val in sorted(series.items(), reverse=True)[:4]
            }
        out.append(entry)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db")
    p.add_argument("--universe-csv", default=str(DATA_DIR / "nifty_500_constituents.csv"))
    p.add_argument("--out", default="data/fundamentals_quality.csv")
    p.add_argument("--spot", default="RBLBANK,SBIN,TCS,RELIANCE,ITC")
    args = p.parse_args(argv)
    store = IndiaStore(args.db) if args.db else IndiaStore()
    result = analyse(store, industries=_industries(Path(args.universe_csv)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as stream:
        w = csv.DictWriter(stream, fieldnames=QUALITY_COLUMNS)
        w.writeheader()
        w.writerows(result["rows"])
    summary = {k: v for k, v in result.items() if k != "rows"}
    print(json.dumps(summary, indent=2))
    print("\nSpot-check (annual, newest first; money in INR crore, EPS in INR). Compare with the company's published results:")
    print(json.dumps(spot_check(store, [s for s in args.spot.split(",") if s.strip()]), indent=2))
    print(f"\nPer-symbol detail: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Transparent rule-based financial health flags over sourced statement views.

These are screening heuristics, not investment advice. Values are provider data in
INR crore (EPS in INR/share); missing metrics never trigger a flag.
"""
from __future__ import annotations
from typing import Any

RULES = {
    "revenue_decline_streak": 3,
    "debt_to_equity_min": 1.0,
    "interest_coverage_below": 2.0,
    "fcf_negative_years": 3,
    "one_off_profit_multiple": 2.5,
    "margin_shrink_min_pp": 1.0,
}


def _series(view: dict[str, Any], metric: str) -> list[tuple[str, float]]:
    rows = [r for section in (view.get("sections") or {}).values() for r in section]
    row = next((r for r in rows if r.get("metric") == metric), None)
    periods = list(reversed(view.get("periods") or []))
    values = row.get("values", {}) if row else {}
    out = []
    for p in periods:
        try:
            v = float(values[p])
            if v == v and abs(v) != float("inf"):
                out.append((p, v))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _flag(severity: str, code: str, title: str, detail: str, periods: list[str] | None = None) -> dict[str, Any]:
    return {"severity": severity, "code": code, "title": title, "detail": detail, "periods": periods or []}


def evaluate_financial_flags(annual: dict[str, Any], quarterly: dict[str, Any], *, is_financial: bool = False) -> dict[str, Any]:
    red: list[dict[str, Any]] = []
    green: list[dict[str, Any]] = []
    qrev, qni = _series(quarterly, "REVENUE"), _series(quarterly, "NET_INCOME")
    arev, ani = _series(annual, "REVENUE"), _series(annual, "NET_INCOME")
    # Banks use net interest income rather than a potentially incomparable revenue line.
    if is_financial:
        qrev = _series(quarterly, "NET_INTEREST_INCOME") or qrev
        arev = _series(annual, "NET_INTEREST_INCOME") or arev
        qni = _series(quarterly, "NET_INCOME")
        ani = _series(annual, "NET_INCOME")
    def trend(series: list[tuple[str, float]], direction: str, n: int = 3) -> bool:
        tail = series[-n:]
        return len(tail) == n and all((tail[i][1] < tail[i+1][1] if direction == "up" else tail[i][1] > tail[i+1][1]) for i in range(n-1))
    if trend(qrev, "down"):
        label = "Net interest income" if is_financial and _series(quarterly, "NET_INTEREST_INCOME") else "Revenue"
        red.append(_flag("red", "revenue_declining_three_quarters", f"{label} fell 3 quarters in a row", " → ".join(f"{v:,.2f}" for _,v in qrev[-3:]) + " cr", [p for p,_ in qrev[-3:]]))
    if trend(qrev, "up"):
        label = "Net interest income" if is_financial and _series(quarterly, "NET_INTEREST_INCOME") else "Revenue"
        green.append(_flag("green", "revenue_growing_three_quarters", f"{label} rose 3 quarters in a row", " → ".join(f"{v:,.2f}" for _,v in qrev[-3:]) + " cr", [p for p,_ in qrev[-3:]]))
    if qni and qni[-1][1] < 0:
        red.append(_flag("red", "latest_quarter_loss", "Latest quarter reported a net loss", f"{qni[-1][1]:,.2f} cr in {qni[-1][0]}", [qni[-1][0]]))
    elif len(qni) >= 2 and qni[-1][1] < qni[-2][1]:
        red.append(_flag("red", "quarterly_profit_falling", "Net income fell quarter over quarter", f"{qni[-2][1]:,.2f} → {qni[-1][1]:,.2f} cr", [qni[-2][0], qni[-1][0]]))
    elif len(qni) >= 2 and qni[-1][1] > qni[-2][1]:
        green.append(_flag("green", "quarterly_profit_growing", "Net income increased quarter over quarter", f"{qni[-2][1]:,.2f} → {qni[-1][1]:,.2f} cr", [qni[-2][0], qni[-1][0]]))
    # Margin contraction uses aligned revenue and profit periods.
    revmap, nimap = dict(qrev), dict(qni)
    margins = [(p, nimap[p]/v*100) for p,v in qrev if v > 0 and p in nimap]
    if len(margins) >= 2:
        pp = margins[-1][1] - margins[-2][1]
        if pp <= -RULES["margin_shrink_min_pp"]:
            red.append(_flag("red", "net_margin_shrinking", "Net margin is shrinking", f"{margins[-2][1]:.2f}% → {margins[-1][1]:.2f}% ({pp:.2f} percentage points)", [margins[-2][0],margins[-1][0]]))
        elif pp > 0:
            green.append(_flag("green", "net_margin_expanding", "Net margin is expanding", f"{margins[-2][1]:.2f}% → {margins[-1][1]:.2f}% (+{pp:.2f} percentage points)", [margins[-2][0],margins[-1][0]]))
    # Annual profit trend and one-off spike are useful for both industrial and financial companies.
    if len(ani) >= 2 and ani[-1][1] < ani[-2][1]:
        red.append(_flag("red", "annual_profit_falling", "Annual net income declined", f"{ani[-2][1]:,.2f} → {ani[-1][1]:,.2f} cr", [ani[-2][0], ani[-1][0]]))
    elif len(ani) >= 2 and ani[-1][1] > ani[-2][1]:
        green.append(_flag("green", "annual_profit_growing", "Annual net income increased", f"{ani[-2][1]:,.2f} → {ani[-1][1]:,.2f} cr", [ani[-2][0], ani[-1][0]]))
    if is_financial:
        for metric, label in (("TOTAL_ASSETS", "Assets"), ("SHAREHOLDERS_EQUITY", "Equity")):
            series = _series(annual, metric)
            if len(series) >= 2 and series[-1][1] > series[-2][1]:
                green.append(_flag("green", f"financial_{metric.lower()}_growing", f"{label} increased", f"{series[-2][1]:,.2f} → {series[-1][1]:,.2f} cr", [series[-2][0],series[-1][0]]))
            elif len(series) >= 2 and series[-1][1] < series[-2][1]:
                red.append(_flag("red", f"financial_{metric.lower()}_falling", f"{label} declined", f"{series[-2][1]:,.2f} → {series[-1][1]:,.2f} cr", [series[-2][0],series[-1][0]]))
    if not is_financial:
        ebit, interest = _series(annual, "EBIT"), _series(annual, "INTEREST_EXPENSE")
        emap, imap = dict(ebit), dict(interest)
        common_cover = sorted(emap.keys() & imap.keys())
        if common_cover and imap[common_cover[-1]] > 0:
            p = common_cover[-1]; coverage = emap[p] / imap[p]
            if coverage < RULES["interest_coverage_below"]:
                red.append(_flag("red", "low_interest_coverage", "Interest coverage is below 2×", f"EBIT {emap[p]:,.2f} / interest expense {imap[p]:,.2f} = {coverage:.2f}×", [p]))
        opm = []
        opmap, revmap2 = dict(_series(quarterly, "OPERATING_INCOME")), dict(_series(quarterly, "REVENUE"))
        for p in sorted(opmap.keys() & revmap2.keys()):
            if revmap2[p] > 0: opm.append((p, opmap[p] / revmap2[p] * 100))
        if len(opm) >= 2 and opm[-1][1] < opm[-2][1] - RULES["margin_shrink_min_pp"]:
            red.append(_flag("red", "operating_margin_shrinking", "Operating margin is shrinking", f"{opm[-2][1]:.2f}% → {opm[-1][1]:.2f}%", [opm[-2][0],opm[-1][0]]))
        debt, equity = _series(annual, "TOTAL_DEBT"), _series(annual, "SHAREHOLDERS_EQUITY")
        dmap, emap = dict(debt), dict(equity)
        common = sorted(dmap.keys() & emap.keys())
        if common:
            p = common[-1]; ratio = dmap[p]/emap[p] if emap[p] > 0 else None
            if ratio is not None and ratio > RULES["debt_to_equity_min"] and len(common)>=2 and dmap[common[-1]] > dmap[common[-2]]:
                red.append(_flag("red", "debt_rising_high_leverage", "Debt is rising while debt-to-equity exceeds 1", f"Debt {dmap[common[-2]]:,.2f} → {dmap[p]:,.2f} cr; D/E {ratio:.2f}×", [common[-2],p]))
            if all(dmap[k] == 0 for k in common[-2:]) or (len(common)>=2 and dmap[common[-1]] < dmap[common[-2]]):
                green.append(_flag("green", "debt_zero_or_falling", "Debt is zero or falling", " → ".join(f"{dmap[k]:,.2f}" for k in common[-2:]) + " cr", common[-2:]))
        cash, netdebt = _series(annual, "CASH_AND_EQUIVALENTS"), _series(annual, "NET_DEBT")
        if netdebt and netdebt[-1][1] < 0:
            green.append(_flag("green", "net_cash_position", "Company has a net cash position", f"Net debt {netdebt[-1][1]:,.2f} cr", [netdebt[-1][0]]))
        elif cash and debt and cash[-1][1] > debt[-1][1]:
            green.append(_flag("green", "cash_exceeds_debt", "Cash exceeds total debt", f"Cash {cash[-1][1]:,.2f} vs debt {debt[-1][1]:,.2f} cr", [cash[-1][0],debt[-1][0]]))
        ocf = _series(quarterly, "OPERATING_CASH_FLOW")
        if ocf and ocf[-1][1] < 0:
            red.append(_flag("red", "negative_operating_cash_flow", "Operating cash flow is negative", f"{ocf[-1][1]:,.2f} cr in {ocf[-1][0]}", [ocf[-1][0]]))
        fcf = _series(annual, "FREE_CASH_FLOW")
        if len(fcf) >= RULES["fcf_negative_years"] and all(v < 0 for _,v in fcf[-RULES["fcf_negative_years"]:]):
            red.append(_flag("red", "multi_year_negative_fcf", "Free cash flow has been negative for several years", " → ".join(f"{v:,.2f}" for _,v in fcf[-3:]) + " cr", [p for p,_ in fcf[-3:]]))
        if ocf and qni and ocf[-1][1] > qni[-1][1] > 0:
            green.append(_flag("green", "cash_flow_exceeds_profit", "Operating cash flow exceeds net income", f"OCF {ocf[-1][1]:,.2f} vs net income {qni[-1][1]:,.2f} cr", [ocf[-1][0],qni[-1][0]]))
    # Large profit jump compared with prior positive annual history is a caution, not a red flag.
    if len(ani) >= 3 and ani[-2][1] > 0 and ani[-1][1] > max(ani[-2][1] * RULES["one_off_profit_multiple"], sorted(v for _,v in ani[:-1] if v > 0)[-1] * RULES["one_off_profit_multiple"] if any(v > 0 for _,v in ani[:-1]) else float("inf")):
        red.append(_flag("warning", "possible_one_off_profit_spike", "Possible one-off profit spike", f"Net income jumped to {ani[-1][1]:,.2f} cr from {ani[-2][1]:,.2f} cr; check for exceptional items", [ani[-2][0],ani[-1][0]]))
    return {"symbol": annual.get("symbol") or quarterly.get("symbol"), "is_financial": is_financial,
            "red_flags": red, "green_flags": green, "rules": RULES,
            "disclaimer": "Simple screens on provider data, not investment advice. Thresholds are configurable in tradingagents/india/financial_flags.py. Missing metrics are not inferred."}

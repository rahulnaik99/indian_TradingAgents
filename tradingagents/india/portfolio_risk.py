"""Deterministic portfolio-level risk checks for the India research workflow.

Only calculate exposure when both holdings and a usable price are supplied.
Thresholds are caller-configured; the module never invents account limits.
"""
from __future__ import annotations

from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def assess_portfolio_risk(*, symbol: str, portfolio: dict[str, Any] | None,
                          current_price: Any = None,
                          limits: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assess known position exposure without assuming missing fields are zero.

    Supported optional limits (percent values from 0 to 100):
    ``max_position_value_pct`` of net liquidation value and
    ``max_gross_exposure_pct`` of net liquidation value.
    ``net_liquidation_value`` may be supplied explicitly in portfolio; otherwise
    it is derived only when cash and prices for every position are known.
    """
    if portfolio is None:
        return {"status": "not_provided", "symbol": symbol.upper(), "checks": [],
                "warnings": ["Portfolio context was not provided; portfolio exposure cannot be assessed."]}
    positions = portfolio.get("positions")
    if not isinstance(positions, list):
        positions = []
    symbol = symbol.strip().upper()
    matched = next((p for p in positions if isinstance(p, dict) and str(p.get("ticker", "")).strip().upper() == symbol), None)
    quantity = _number(matched.get("quantity")) if matched else 0.0
    price = _number(current_price)
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    if matched is None:
        checks.append({"name": "target_position", "status": "no_position_reported", "symbol": symbol})
    elif quantity is None:
        warnings.append(f"Quantity for {symbol} is invalid; target exposure was not calculated.")
    elif price is None or price <= 0:
        warnings.append(f"A valid current price for {symbol} was not supplied; target market value was not calculated.")
    else:
        checks.append({"name": "target_position", "status": "calculated", "symbol": symbol,
                       "quantity": quantity, "current_price": price,
                       "market_value": quantity * price, "direction": "short" if quantity < 0 else "long" if quantity > 0 else "flat"})

    cash = _number(portfolio.get("cash"))
    nl_value = _number(portfolio.get("net_liquidation_value"))
    if nl_value is None and cash is not None:
        total = cash
        all_prices_known = True
        for pos in positions:
            if not isinstance(pos, dict):
                all_prices_known = False
                break
            qty = _number(pos.get("quantity"))
            ticker = str(pos.get("ticker", "")).strip().upper()
            pos_price = price if ticker == symbol else _number(pos.get("current_price"))
            if qty is None or pos_price is None or pos_price <= 0:
                all_prices_known = False
                break
            total += qty * pos_price
        if all_prices_known:
            nl_value = total
    if nl_value is None or nl_value <= 0:
        warnings.append("Net liquidation value is unavailable; percentage exposure limits were not evaluated.")
    else:
        checks.append({"name": "net_liquidation_value", "status": "calculated", "value": nl_value,
                       "currency": portfolio.get("currency")})
        gross = 0.0
        gross_known = True
        for pos in positions:
            if not isinstance(pos, dict):
                gross_known = False; break
            qty = _number(pos.get("quantity")); ticker = str(pos.get("ticker", "")).strip().upper()
            pos_price = price if ticker == symbol else _number(pos.get("current_price"))
            if qty is None or pos_price is None or pos_price <= 0:
                gross_known = False; break
            gross += abs(qty * pos_price)
        if gross_known:
            gross_pct = gross / nl_value * 100
            checks.append({"name": "gross_exposure", "status": "calculated", "gross_market_value": gross,
                           "gross_exposure_pct": round(gross_pct, 4)})
            max_gross = _number((limits or {}).get("max_gross_exposure_pct"))
            if max_gross is not None and not 0 <= max_gross <= 1000:
                warnings.append("max_gross_exposure_pct is outside the supported 0–1000 range and was ignored.")
            elif max_gross is not None and gross_pct > max_gross:
                checks.append({"name": "max_gross_exposure_pct", "status": "breach", "limit_pct": max_gross,
                               "observed_pct": round(gross_pct, 4)})
        else:
            warnings.append("Gross exposure is incomplete because one or more holdings lack current prices or valid quantities.")
        target_check = next((c for c in checks if c.get("name") == "target_position" and c.get("status") == "calculated"), None)
        max_position = _number((limits or {}).get("max_position_value_pct"))
        if target_check and max_position is not None:
            if not 0 <= max_position <= 100:
                warnings.append("max_position_value_pct is outside the supported 0–100 range and was ignored.")
            else:
                pct = abs(target_check["market_value"]) / nl_value * 100
                if pct > max_position:
                    checks.append({"name": "max_position_value_pct", "status": "breach", "limit_pct": max_position,
                                   "observed_pct": round(pct, 4), "symbol": symbol})
    breaches = [c for c in checks if c.get("status") == "breach"]
    return {"status": "limit_breach" if breaches else "assessed_with_warnings" if warnings else "assessed",
            "symbol": symbol, "checks": checks, "breach_count": len(breaches), "warnings": warnings,
            "limits_applied": {k: v for k, v in (limits or {}).items() if k in {"max_position_value_pct", "max_gross_exposure_pct"}}}

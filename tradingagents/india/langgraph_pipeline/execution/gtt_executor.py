"""Fail-closed trade sizing and GTT execution.

The analysis graph itself never talks to the broker. The API calls this module
only after a final BUY/SELL verdict, approved risk review, and PLACE_ORDER=true.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from math import floor, isfinite
from typing import Any, Callable

from tradingagents.india.langgraph_pipeline.execution.kite_mcp import KiteMCPClient


@dataclass(frozen=True)
class ExecutionConfig:
    place_order: bool = False
    amount_per_trade: float = 10_000.0
    max_risk_per_trade: float = 500.0
    product: str = "CNC"
    order_type: str = "LIMIT"
    gtt_tool_name: str = "place_gtt_order"

    @classmethod
    def from_env(cls) -> "ExecutionConfig":
        flag = os.getenv("PLACE_ORDER", "false").strip().lower()
        if flag not in {"true", "false", "1", "0", "yes", "no"}:
            raise ValueError("PLACE_ORDER must be true/false")
        amount = _positive_float(os.getenv("AMOUNT_PER_TRADE", "10000"), "AMOUNT_PER_TRADE")
        risk = _positive_float(os.getenv("MAX_RISK_PER_TRADE", "500"), "MAX_RISK_PER_TRADE")
        product = os.getenv("KITE_PRODUCT", "CNC").strip().upper()
        if product not in {"CNC", "MIS", "NRML"}:
            raise ValueError("KITE_PRODUCT must be CNC, MIS, or NRML")
        return cls(flag in {"true", "1", "yes"}, amount, risk, product,
                   os.getenv("KITE_ORDER_TYPE", "LIMIT").strip().upper(),
                   os.getenv("KITE_GTT_TOOL_NAME", "place_gtt_order").strip())


def _positive_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return number


def calculate_quantity(*, entry: float, stop_loss: float, amount_per_trade: float,
                       max_risk_per_trade: float, lot_size: int = 1) -> dict[str, Any]:
    """Calculate whole-unit quantity bounded by notional and stop-loss risk."""
    entry = _positive_float(entry, "entry")
    stop_loss = _positive_float(stop_loss, "stop_loss")
    amount = _positive_float(amount_per_trade, "amount_per_trade")
    max_risk = _positive_float(max_risk_per_trade, "max_risk_per_trade")
    if isinstance(lot_size, bool) or not isinstance(lot_size, int) or lot_size < 1:
        raise ValueError("lot_size must be a positive integer")
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        raise ValueError("entry and stop_loss must differ")
    capital_units = floor(amount / entry)
    risk_units = floor(max_risk / stop_distance)
    raw_units = min(capital_units, risk_units)
    quantity = (raw_units // lot_size) * lot_size
    return {"quantity": quantity, "capital_quantity_limit": capital_units,
            "risk_quantity_limit": risk_units, "notional": round(quantity * entry, 2),
            "estimated_stop_risk": round(quantity * stop_distance, 2),
            "lot_size": lot_size}


def _last_fyers_price(state: dict[str, Any]) -> tuple[float | None, str | None]:
    """Use the most recent completed candle already collected through FYERS."""
    market = state.get("market_data") or {}
    series = market.get("candles_by_timeframe") or {}
    candidates = []
    for timeframe, candles in series.items():
        if not isinstance(candles, list):
            continue
        for candle in candles:
            if isinstance(candle, dict) and isinstance(candle.get("close"), (int, float)):
                candidates.append((str(candle.get("close_time") or ""), float(candle["close"]), timeframe))
    if not candidates:
        return None, None
    timestamp, price, timeframe = max(candidates, key=lambda item: item[0])
    return price, timestamp


def _eligible(state: dict[str, Any]) -> str | None:
    request = state.get("request") or {}
    verdict = state.get("final_verdict") or {}
    risk = state.get("risk_analysis") or {}
    proposal = state.get("trader_analysis") or {}
    if os.getenv("PLACE_ORDER", "false").strip().lower() not in {"true", "1", "yes"}:
        return "PLACE_ORDER is false; order submission skipped."
    if request.get("analysis_mode") != "latest":
        return "Only analysis_mode='latest' can trigger live execution."
    if verdict.get("verdict") not in {"BUY", "SELL"}:
        return "Final verdict is not BUY/SELL; no order submitted."
    if verdict.get("risk_status") != "approved_for_review" or risk.get("status") != "approved_for_review":
        return "Risk review is not approved; no order submitted."
    if risk.get("hard_blocks"):
        return "Risk review contains hard blocks; no order submitted."
    if proposal.get("action") != verdict.get("verdict"):
        return "Final verdict does not match the validated Trader Agent proposal."
    return None


async def execute_trade_decision(state: dict[str, Any], *, client: Any | None = None,
                                 config: ExecutionConfig | None = None,
                                 lot_size_resolver: Callable[[str, str], int] | None = None) -> dict[str, Any]:
    """Place a single-entry GTT via Kite MCP after deterministic validation.

    Kite GTT supports a single trigger or a two-leg OCO, not an atomic entry +
    stop-loss + target triple. Therefore this submits only the entry GTT and
    marks exit protection as pending; deployments must run the reconciliation
    worker before enabling live orders if automatic exit-GTT attachment is
    required. This explicit state prevents claiming that stop/target are armed.
    """
    config = config or ExecutionConfig.from_env()
    reason = _eligible(state)
    if reason:
        status = "ORDER_SKIPPED" if "PLACE_ORDER is false" in reason else "EXECUTION_REJECTED"
        return {"execution_result": {"status": status, "submitted": False, "reason": reason,
                "broker": "kite_mcp", "market_data_provider": "fyers"}}

    proposal = state.get("trader_analysis") or {}
    request = state.get("request") or {}
    entry, stop, target = proposal.get("entry"), proposal.get("stop_loss"), proposal.get("target")
    direction = proposal.get("action")
    if direction not in {"BUY", "SELL"} or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(float(x)) and float(x) > 0 for x in (entry, stop, target)):
        return {"execution_result": {"status": "EXECUTION_REJECTED", "submitted": False,
                "reason": "Valid positive entry, stop-loss, and target are required.", "broker": "kite_mcp"}}
    if (direction == "BUY" and not float(stop) < float(entry) < float(target)) or (direction == "SELL" and not float(target) < float(entry) < float(stop)):
        return {"execution_result": {"status": "EXECUTION_REJECTED", "submitted": False,
                "reason": "Entry/stop/target geometry is invalid.", "broker": "kite_mcp"}}

    price, candle_time = _last_fyers_price(state)
    if price is None or price <= 0:
        return {"execution_result": {"status": "EXECUTION_REJECTED", "submitted": False,
                "reason": "No valid completed FYERS candle available for GTT last_price.", "broker": "kite_mcp"}}
    # A quote older than 20 minutes is too stale for live entry submission.
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        stamp = datetime.fromisoformat(str(candle_time).replace("Z", "+00:00"))
        now = datetime.now(stamp.tzinfo) if stamp.tzinfo else datetime.now(ZoneInfo("Asia/Kolkata"))
        if stamp.tzinfo is None or now - stamp > timedelta(minutes=20) or stamp > now + timedelta(minutes=1):
            return {"execution_result": {"status": "EXECUTION_REJECTED", "submitted": False,
                    "reason": "Latest completed FYERS candle is stale or has an invalid timestamp.",
                    "market_data_timestamp": candle_time, "broker": "kite_mcp"}}
    except (TypeError, ValueError, OverflowError):
        return {"execution_result": {"status": "EXECUTION_REJECTED", "submitted": False,
                "reason": "FYERS candle timestamp is invalid.", "broker": "kite_mcp"}}

    lot_size = lot_size_resolver(request.get("symbol", ""), request.get("exchange", "NSE")) if lot_size_resolver else 1
    try:
        sizing = calculate_quantity(entry=float(entry), stop_loss=float(stop),
                                    amount_per_trade=config.amount_per_trade,
                                    max_risk_per_trade=config.max_risk_per_trade, lot_size=lot_size)
    except ValueError as exc:
        return {"execution_result": {"status": "EXECUTION_REJECTED", "submitted": False,
                "reason": str(exc), "broker": "kite_mcp"}}
    if sizing["quantity"] < 1:
        return {"execution_result": {"status": "EXECUTION_REJECTED", "submitted": False,
                "reason": "Risk/capital limits produce zero quantity.", "sizing": sizing, "broker": "kite_mcp"}}

    # Kite GTT cannot atomically encode entry + stop-loss + target in one request.
    # Refuse an unprotected live entry by default; an operator must explicitly
    # opt in while a fill-reconciliation/exit-GTT worker is not configured.
    require_stop = os.getenv("REQUIRE_STOP_LOSS", "true").strip().lower() in {"true", "1", "yes"}
    allow_unprotected = os.getenv("ALLOW_UNPROTECTED_ENTRY_GTT", "false").strip().lower() in {"true", "1", "yes"}
    if require_stop and not allow_unprotected:
        return {"execution_result": {"status": "EXECUTION_REJECTED", "submitted": False,
                "reason": "Entry + stop-loss + target cannot be armed atomically as one Kite GTT. "
                          "A fill-reconciliation worker must attach OCO exits; no order was submitted.",
                "sizing": sizing, "broker": "kite_mcp", "market_data_provider": "fyers"}}

    # Do not let a stale caller config override the explicit environment gate.
    env_enabled = os.getenv("PLACE_ORDER", "false").strip().lower() in {"true", "1", "yes"}
    if not config.place_order or not env_enabled:
        return {"execution_result": {"status": "ORDER_SKIPPED", "submitted": False,
                "reason": "Live execution is disabled by PLACE_ORDER.", "sizing": sizing, "broker": "kite_mcp"}}
    if client is None:
        client = KiteMCPClient()

    transaction_type = "BUY" if direction == "BUY" else "SELL"
    args = {
        "trigger_type": "single",
        "tradingsymbol": str(request.get("symbol", "")).upper(),
        "exchange": str(request.get("exchange", "NSE")).upper(),
        "trigger_values": [float(entry)],
        "last_price": float(price),
        "orders": [{"transaction_type": transaction_type, "quantity": sizing["quantity"],
                    "order_type": config.order_type, "product": config.product, "price": float(entry)}],
    }
    try:
        if hasattr(client, "call_tool"):
            result = client.call_tool(config.gtt_tool_name, args)
            if hasattr(result, "__await__"):
                result = await result
        else:
            raise TypeError("Kite MCP client must provide call_tool(name, arguments)")
    except Exception as exc:
        return {"execution_result": {"status": "SUBMISSION_FAILED", "submitted": False,
                "reason": f"Kite MCP submission failed ({type(exc).__name__}).", "sizing": sizing,
                "market_data_provider": "fyers", "broker": "kite_mcp"}}

    return {"execution_result": {"status": "GTT_SUBMITTED", "submitted": True,
            "gtt_kind": "single_entry_trigger", "broker": "kite_mcp", "tool": config.gtt_tool_name,
            "broker_response": result, "sizing": sizing, "market_data_provider": "fyers",
            "market_data_timestamp": candle_time, "entry": float(entry), "stop_loss": float(stop),
            "target": float(target), "exit_protection_status": "PENDING_AFTER_ENTRY_FILL",
            "warning": "Entry GTT submitted. Stop-loss/target OCO is not yet armed; reconcile entry fill and attach OCO exits before relying on protection."}}


def execute_trade_decision_sync(state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Synchronous wrapper for FastAPI's synchronous route."""
    return asyncio.run(execute_trade_decision(state, **kwargs))

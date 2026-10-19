"""Deterministic paper order service. It cannot submit live broker orders."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from tradingagents.india.storage import IndiaStore


class PaperTradingError(ValueError):
    pass


class PaperBroker:
    def __init__(self, store: IndiaStore):
        self.store = store

    def place_order(self, *, symbol: str, side: str, quantity: int, reference_price: float,
                    stop_loss: float | None = None, target: float | None = None,
                    note: str | None = None) -> dict[str, Any]:
        symbol, side = symbol.strip().upper(), side.strip().lower()
        if not symbol or side not in {"buy", "sell"}:
            raise PaperTradingError("symbol is required and side must be buy or sell")
        if isinstance(quantity, bool) or quantity <= 0 or quantity > 1_000_000:
            raise PaperTradingError("quantity must be between 1 and 1,000,000")
        if not isinstance(reference_price, (int, float)) or reference_price <= 0:
            raise PaperTradingError("reference_price must be positive")
        if stop_loss is not None and stop_loss <= 0:
            raise PaperTradingError("stop_loss must be positive")
        if target is not None and target <= 0:
            raise PaperTradingError("target must be positive")
        if side == "buy" and stop_loss is not None and stop_loss >= reference_price:
            raise PaperTradingError("for a buy, stop_loss must be below reference_price")
        if side == "buy" and target is not None and target <= reference_price:
            raise PaperTradingError("for a buy, target must be above reference_price")
        if side == "sell":
            if stop_loss is not None and stop_loss <= reference_price:
                raise PaperTradingError("for a sell/long close, stop_loss must be above reference_price")
            if target is not None and target >= reference_price:
                raise PaperTradingError("for a sell/long close, target must be below reference_price")
            held = self.portfolio()["positions"].get(symbol, {}).get("quantity", 0)
            if quantity > held:
                raise PaperTradingError(f"Cannot sell {quantity} shares in paper mode; current long position is {held}.")
        now = datetime.now(timezone.utc).isoformat()
        order = {"order_id": uuid.uuid4().hex, "symbol": symbol, "side": side, "quantity": int(quantity),
                 "reference_price": float(reference_price), "stop_loss": stop_loss, "target": target,
                 "status": "PAPER_FILLED", "created_at": now, "filled_at": now,
                 "note": note, "broker": "paper", "live_order": False,
                 "message": "Simulated fill at supplied reference price; not an actual market fill."}
        self.store.save_paper_order(order)
        return order

    def portfolio(self) -> dict[str, Any]:
        orders = self.store.list_paper_orders()
        positions: dict[str, dict[str, float]] = {}
        realized = 0.0
        # Long-only FIFO lot ledger; sells beyond current long quantity are rejected in this view.
        lots: dict[str, list[list[float]]] = {}
        for order in sorted(orders, key=lambda x: x["created_at"]):
            symbol, side = order["symbol"], order["side"]
            qty, price = float(order["quantity"]), float(order["reference_price"])
            lots.setdefault(symbol, [])
            if side == "buy":
                lots[symbol].append([qty, price])
            else:
                remaining = qty
                while remaining > 0 and lots[symbol]:
                    lot_qty, lot_price = lots[symbol][0]
                    consumed = min(remaining, lot_qty)
                    realized += (price - lot_price) * consumed
                    lot_qty -= consumed
                    remaining -= consumed
                    if lot_qty <= 1e-9:
                        lots[symbol].pop(0)
                    else:
                        lots[symbol][0][0] = lot_qty
        for symbol, symbol_lots in lots.items():
            qty = sum(lot[0] for lot in symbol_lots)
            cost = sum(lot[0] * lot[1] for lot in symbol_lots)
            if qty:
                positions[symbol] = {"quantity": qty, "average_cost": cost / qty, "cost_value": cost}
        return {"mode": "paper", "positions": positions, "realized_pnl_before_costs": round(realized, 2),
                "orders_count": len(orders), "live_trading_enabled": False,
                "note": "Paper P&L excludes brokerage, taxes, slippage, corporate actions and mark-to-market pricing."}

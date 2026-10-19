"""FYERS v3 live-order adapter with multi-layer opt-in and strict safety gates.

This module never places an order unless both server configuration gates and a
per-request confirmation phrase are satisfied. Order submissions are not retried:
a timeout can mean the broker received the order, so check the order book before
attempting another submission.
"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any, Mapping

import requests


class FyersOrderError(RuntimeError):
    pass


class LiveTradingDisabled(FyersOrderError):
    pass


class FyersOrderClient:
    provider_name = "fyers"
    CONFIRMATION = "PLACE_REAL_ORDER"
    SERVER_CONFIRMATION = "I_UNDERSTAND_REAL_MONEY_RISK"

    def __init__(self, app_id: str, access_token: str, *, base_url: str = "https://api-t1.fyers.in/api/v3",
                 session: Any | None = None, timeout_seconds: float = 12.0,
                 max_order_qty: int = 100, max_order_value: float = 50000.0):
        if not app_id.strip() or not access_token.strip():
            raise ValueError("FYERS app ID and access token are required")
        self.app_id = app_id.strip()
        self._access_token = access_token.strip()
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.max_order_qty = max_order_qty
        self.max_order_value = max_order_value

    @classmethod
    def from_env(cls, *, confirmation_phrase: str) -> "FyersOrderClient":
        if os.getenv("ENABLE_LIVE_TRADING", "false").lower() not in {"1", "true", "yes"}:
            raise LiveTradingDisabled("Live trading is disabled. Set ENABLE_LIVE_TRADING=true only after reviewing safeguards.")
        if os.getenv("LIVE_TRADING_CONFIRMATION", "") != cls.SERVER_CONFIRMATION:
            raise LiveTradingDisabled("Live trading server confirmation is missing. Set LIVE_TRADING_CONFIRMATION to the documented exact phrase locally.")
        if confirmation_phrase != cls.CONFIRMATION:
            raise LiveTradingDisabled(f"Every live order operation requires confirmation_phrase={cls.CONFIRMATION!r}.")
        if not os.getenv("FYERS_APP_ID") or not os.getenv("FYERS_ACCESS_TOKEN"):
            raise FyersOrderError("FYERS_APP_ID and FYERS_ACCESS_TOKEN must be configured for live order operations")
        try:
            max_qty = int(os.getenv("LIVE_MAX_ORDER_QTY", "100"))
            max_value = float(os.getenv("LIVE_MAX_ORDER_VALUE_INR", "50000"))
        except ValueError as exc:
            raise FyersOrderError("LIVE_MAX_ORDER_QTY and LIVE_MAX_ORDER_VALUE_INR must be numeric") from exc
        if max_qty <= 0 or max_value <= 0:
            raise FyersOrderError("Live order safety limits must be positive")
        return cls(os.getenv("FYERS_APP_ID", ""), os.getenv("FYERS_ACCESS_TOKEN", ""),
                   base_url=os.getenv("FYERS_ORDER_BASE_URL", "https://api-t1.fyers.in/api/v3"),
                   max_order_qty=max_qty, max_order_value=max_value)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"{self.app_id}:{self._access_token}", "Content-Type": "application/json",
                   "Accept": "application/json", "User-Agent": "TradingAgents-India/0.2"}
        try:
            response = self.session.request(method, f"{self.base_url}/{path.lstrip('/')}", json=payload,
                                            headers=headers, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            # Never retry write operations: broker acceptance may be ambiguous after a timeout.
            raise FyersOrderError(f"FYERS order request failed ({type(exc).__name__}); reconcile order book before retrying.") from exc
        if response.status_code in (401, 403):
            raise FyersOrderError("FYERS rejected authentication or order permission; no order confirmation received.")
        if response.status_code == 429:
            raise FyersOrderError("FYERS order API rate limit reached; inspect order book before any retry.")
        if response.status_code >= 400:
            raise FyersOrderError(f"FYERS order API returned HTTP {response.status_code}; inspect broker order book.")
        try:
            body = response.json()
        except (ValueError, AttributeError) as exc:
            raise FyersOrderError("FYERS order API returned non-JSON response; reconcile order book before retrying.") from exc
        if not isinstance(body, Mapping):
            raise FyersOrderError("FYERS order response was not a JSON object")
        if str(body.get("s", "")).lower() != "ok":
            # Avoid echoing raw provider response because it may contain user-specific details.
            message = str(body.get("message") or body.get("code") or "unknown broker error")[:300]
            raise FyersOrderError(f"FYERS did not confirm the operation: {message}")
        return dict(body)

    @staticmethod
    def vendor_symbol(symbol: str) -> str:
        cleaned = symbol.strip().upper()
        if cleaned.startswith("NSE:") and cleaned.endswith("-EQ"):
            cleaned = cleaned[4:-3]
        if not re.fullmatch(r"[A-Z0-9&_-]+", cleaned):
            raise ValueError("Invalid NSE equity symbol")
        return f"NSE:{cleaned}-EQ"

    def place_limit_order(self, *, symbol: str, side: str, quantity: int, limit_price: float) -> dict[str, Any]:
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if isinstance(quantity, bool) or quantity < 1 or quantity > self.max_order_qty:
            raise ValueError(f"quantity must be between 1 and configured safety limit {self.max_order_qty}")
        if not isinstance(limit_price, (int, float)) or limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if quantity * float(limit_price) > self.max_order_value:
            raise ValueError(f"order value exceeds configured safety limit INR {self.max_order_value:.2f}")
        tag = "TAI" + uuid.uuid4().hex[:12]
        payload = {"symbol": self.vendor_symbol(symbol), "qty": int(quantity), "type": 1,
                   "side": 1 if side == "buy" else -1, "productType": "CNC", "limitPrice": float(limit_price),
                   "stopPrice": 0, "disclosedQty": 0, "validity": "DAY", "offlineOrder": False,
                   "stopLoss": 0, "takeProfit": 0, "orderTag": tag, "isSliceOrder": False}
        body = self._request("POST", "/orders/sync", payload)
        return {"provider": "fyers", "mode": "live", "symbol": symbol.upper(), "side": side,
                "quantity": quantity, "limit_price": float(limit_price), "order_id": body.get("id"),
                "broker_code": body.get("code"), "message": body.get("message"), "order_tag": tag,
                "live_order": True, "warning": "Broker acceptance is not proof of execution; reconcile status and fills."}

    def modify_limit_order(self, *, order_id: str, quantity: int, limit_price: float) -> dict[str, Any]:
        if not order_id.strip() or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", order_id):
            raise ValueError("invalid order_id")
        if isinstance(quantity, bool) or quantity < 1 or quantity > self.max_order_qty:
            raise ValueError(f"quantity must be between 1 and configured safety limit {self.max_order_qty}")
        if limit_price <= 0 or quantity * limit_price > self.max_order_value:
            raise ValueError("limit_price must be positive and order value within configured safety limit")
        body = self._request("PATCH", "/orders/sync", {"id": order_id, "type": 1,
                                                           "limitPrice": float(limit_price), "qty": int(quantity), "disclosedQty": 0})
        return {"provider": "fyers", "mode": "live", "order_id": order_id,
                "broker_code": body.get("code"), "message": body.get("message"), "live_order": True}

    def cancel_order(self, *, order_id: str) -> dict[str, Any]:
        if not order_id.strip() or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", order_id):
            raise ValueError("invalid order_id")
        body = self._request("DELETE", "/orders/sync", {"id": order_id})
        return {"provider": "fyers", "mode": "live", "order_id": order_id,
                "broker_code": body.get("code"), "message": body.get("message"), "live_order": True,
                "warning": "Cancellation is not complete until broker status confirms it."}

    def orderbook(self) -> dict[str, Any]:
        body = self._request("GET", "/orders")
        # Do not include any credentials in the response; broker orderbook is user data.
        return {"provider": "fyers", "mode": "live", "orders": body.get("orderBook") or body.get("data") or [],
                "live_order": False, "note": "Orderbook is broker-reported state; verify fills and pending quantities."}

import json

import pytest

from tradingagents.india.providers.fyers_orders import FyersOrderClient, FyersOrderError, LiveTradingDisabled


class Response:
    status_code = 200
    def json(self):
        return {"s": "ok", "code": 1101, "id": "ORD123", "message": "Order submitted"}


class Session:
    def __init__(self):
        self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response()


def test_live_orders_disabled_without_explicit_server_gates(monkeypatch):
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    monkeypatch.delenv("LIVE_TRADING_CONFIRMATION", raising=False)
    with pytest.raises(LiveTradingDisabled):
        FyersOrderClient.from_env(confirmation_phrase="PLACE_REAL_ORDER")


def test_live_order_requires_per_request_confirmation(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_TRADING_CONFIRMATION", FyersOrderClient.SERVER_CONFIRMATION)
    monkeypatch.setenv("FYERS_APP_ID", "test-app")
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", "test-token")
    with pytest.raises(LiveTradingDisabled):
        FyersOrderClient.from_env(confirmation_phrase="no")


def test_limit_order_payload_is_capped_and_uses_cnc_delivery():
    session = Session()
    client = FyersOrderClient("app", "token", session=session, max_order_qty=10, max_order_value=10000)
    result = client.place_limit_order(symbol="SBIN", side="buy", quantity=2, limit_price=500)
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/orders/sync")
    assert kwargs["json"]["symbol"] == "NSE:SBIN-EQ"
    assert kwargs["json"]["productType"] == "CNC"
    assert kwargs["json"]["type"] == 1
    assert result["order_id"] == "ORD123"
    assert result["live_order"] is True


def test_limit_order_safety_limits_reject_before_network():
    session = Session()
    client = FyersOrderClient("app", "token", session=session, max_order_qty=2, max_order_value=1000)
    with pytest.raises(ValueError):
        client.place_limit_order(symbol="SBIN", side="buy", quantity=3, limit_price=100)
    with pytest.raises(ValueError):
        client.place_limit_order(symbol="SBIN", side="buy", quantity=2, limit_price=600)
    assert session.calls == []

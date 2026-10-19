from __future__ import annotations

from datetime import datetime, timezone
import asyncio

import pytest

from tradingagents.india.langgraph_pipeline.execution.gtt_executor import (
    ExecutionConfig,
    calculate_quantity,
    execute_trade_decision,
)


def test_quantity_respects_capital_and_risk():
    result = calculate_quantity(entry=250, stop_loss=245, amount_per_trade=10_000,
                                max_risk_per_trade=500)
    assert result["quantity"] == 40
    assert result["notional"] == 10_000
    assert result["estimated_stop_risk"] == 200


def test_quantity_rounds_down_to_lot_size():
    result = calculate_quantity(entry=250, stop_loss=245, amount_per_trade=10_000,
                                max_risk_per_trade=500, lot_size=10)
    assert result["quantity"] == 40


def test_invalid_prices_fail():
    with pytest.raises(ValueError):
        calculate_quantity(entry=100, stop_loss=100, amount_per_trade=1000, max_risk_per_trade=100)


def test_place_order_false_never_calls_mcp(monkeypatch):
    monkeypatch.setenv("PLACE_ORDER", "false")
    class Client:
        called = False
        async def call_tool(self, name, arguments):
            self.called = True
            return {"ok": True}
    client = Client()
    result = asyncio.run(execute_trade_decision({"request": {"analysis_mode": "latest"}}, client=client))
    assert result["execution_result"]["status"] == "ORDER_SKIPPED"
    assert not client.called


def test_no_trade_never_calls_mcp_even_when_enabled(monkeypatch):
    monkeypatch.setenv("PLACE_ORDER", "true")
    class Client:
        called = False
        async def call_tool(self, name, arguments):
            self.called = True
            return {"ok": True}
    client = Client()
    result = asyncio.run(execute_trade_decision({"request": {"analysis_mode": "latest"},
        "final_verdict": {"verdict": "NO_TRADE"}}, client=client,
        config=ExecutionConfig(place_order=True)))
    assert result["execution_result"]["submitted"] is False
    assert not client.called


def test_approved_trade_submits_entry_gtt_using_fyers_price(monkeypatch):
    monkeypatch.setenv("PLACE_ORDER", "true")
    monkeypatch.setenv("REQUIRE_STOP_LOSS", "false")
    monkeypatch.setenv("ALLOW_UNPROTECTED_ENTRY_GTT", "true")
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "request": {"symbol": "INFY", "exchange": "NSE", "analysis_mode": "latest"},
        "final_verdict": {"verdict": "BUY", "risk_status": "approved_for_review"},
        "risk_analysis": {"status": "approved_for_review", "hard_blocks": []},
        "trader_analysis": {"action": "BUY", "entry": 250.0, "stop_loss": 245.0, "target": 260.0},
        "market_data": {"candles_by_timeframe": {"1m": [{"close": 249.5, "close_time": now}]}}
    }
    class Client:
        calls = []
        async def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return {"trigger_id": "test-trigger"}
    client = Client()
    result = asyncio.run(execute_trade_decision(state, client=client,
        config=ExecutionConfig(place_order=True, amount_per_trade=10_000, max_risk_per_trade=500)))
    assert result["execution_result"]["status"] == "GTT_SUBMITTED"
    assert result["execution_result"]["market_data_provider"] == "fyers"
    assert result["execution_result"]["exit_protection_status"] == "PENDING_AFTER_ENTRY_FILL"
    assert client.calls[0][0] == "place_gtt_order"
    assert client.calls[0][1]["last_price"] == 249.5


def test_kite_mcp_defaults_to_hosted_remote_endpoint(monkeypatch):
    from tradingagents.india.langgraph_pipeline.execution.kite_mcp import KiteMCPClient
    monkeypatch.delenv("KITE_MCP_URL", raising=False)
    client = KiteMCPClient()
    assert client.url == "https://mcp.kite.trade/mcp"


def test_kite_gtt_tool_name_matches_official_server_tool(monkeypatch):
    monkeypatch.delenv("KITE_GTT_TOOL_NAME", raising=False)
    config = ExecutionConfig.from_env()
    assert config.gtt_tool_name == "place_gtt_order"

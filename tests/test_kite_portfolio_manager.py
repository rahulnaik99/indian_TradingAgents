from __future__ import annotations

import asyncio
import json

from tradingagents.india.langgraph_pipeline.portfolio_manager import PortfolioManager, _decode_tool_result


class FakeKiteMCP:
    url = "https://mcp.kite.trade/mcp"

    def __init__(self, tools=None, payloads=None):
        self.tools = tools or [
            {"name": "get_holdings"},
            {"name": "get_positions"},
            {"name": "get_margins"},
        ]
        self.payloads = payloads or {}
        self.calls = []

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"tool": name, "is_error": False, "content": [
            {"type": "text", "text": json.dumps(self.payloads.get(name, []))}
        ], "structured_content": None}


def test_portfolio_manager_reads_holdings_positions_and_margins():
    client = FakeKiteMCP(payloads={
        "get_holdings": [{"tradingsymbol": "INFY", "quantity": 5}],
        "get_positions": {"net": [], "day": []},
        "get_margins": {"equity": {"available": {"live_balance": 10000}}},
    })
    result = asyncio.run(PortfolioManager(client).get_portfolio())
    assert result["provider"] == "kite_mcp"
    assert result["holdings_count"] == 1
    assert result["holdings"][0]["tradingsymbol"] == "INFY"
    assert result["positions"]["data"]["net"] == []
    assert result["margins"]["data"]["equity"]["available"]["live_balance"] == 10000
    assert [call[0] for call in client.calls] == ["get_holdings", "get_positions", "get_margins"]


def test_portfolio_manager_does_not_fail_if_optional_tools_missing():
    client = FakeKiteMCP(tools=[{"name": "get_holdings"}], payloads={"get_holdings": []})
    result = asyncio.run(PortfolioManager(client).get_portfolio())
    assert result["holdings_count"] == 0
    assert result["positions"]["available"] is False
    assert result["margins"]["available"] is False


def test_portfolio_manager_requires_holdings_tool():
    import pytest
    from tradingagents.india.langgraph_pipeline.portfolio_manager import PortfolioManagerError
    client = FakeKiteMCP(tools=[{"name": "get_profile"}])
    with pytest.raises(PortfolioManagerError, match="get_holdings"):
        asyncio.run(PortfolioManager(client).get_portfolio())


def test_decode_tool_result_preserves_non_json_text():
    assert _decode_tool_result({"structured_content": None, "content": [{"text": "Login required"}]}) == {"text": "Login required"}

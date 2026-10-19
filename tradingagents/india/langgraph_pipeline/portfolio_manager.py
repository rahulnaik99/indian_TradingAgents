"""Kite-backed portfolio manager for account holdings, positions, and margins.

This module is deliberately separate from FYERS market-data collection. It uses
Kite MCP only for account/portfolio state and never places or modifies orders.
"""
from __future__ import annotations

import json
from typing import Any

from tradingagents.india.langgraph_pipeline.execution.kite_mcp import KiteMCPClient, KiteMCPError


class PortfolioManagerError(RuntimeError):
    """Raised when Kite portfolio information cannot be fetched or decoded."""


def _decode_tool_result(result: dict[str, Any]) -> Any:
    """Convert common MCP result formats to useful JSON/Python data."""
    structured = result.get("structured_content")
    if structured is not None:
        return structured
    content = result.get("content") or []
    texts = [item.get("text") for item in content if isinstance(item, dict) and item.get("text")]
    if not texts:
        return content
    joined = "\n".join(texts).strip()
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, TypeError):
        # Preserve the server's human-readable result without guessing its shape.
        return {"text": joined}


class PortfolioManager:
    """Fetch current holdings, positions and margins from the configured Kite MCP."""

    def __init__(self, client: Any | None = None) -> None:
        self.client = client or KiteMCPClient()

    async def get_portfolio(self, *, include_positions: bool = True,
                            include_margins: bool = True) -> dict[str, Any]:
        try:
            tools = await self.client.list_tools()
        except Exception as exc:
            raise PortfolioManagerError(f"Kite MCP tool discovery failed ({type(exc).__name__}).") from exc

        available = {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}
        if "get_holdings" not in available:
            raise PortfolioManagerError("Kite MCP does not expose required tool 'get_holdings'.")

        async def call_if_available(name: str, required: bool = False) -> Any:
            if name not in available:
                if required:
                    raise PortfolioManagerError(f"Kite MCP does not expose required tool {name!r}.")
                return {"available": False, "reason": f"Tool {name!r} is not exposed by this MCP endpoint."}
            try:
                raw = await self.client.call_tool(name, {})
                if raw.get("is_error"):
                    raise PortfolioManagerError(f"Kite MCP tool {name!r} returned an error.")
                return {"available": True, "data": _decode_tool_result(raw)}
            except PortfolioManagerError:
                raise
            except Exception as exc:
                raise PortfolioManagerError(f"Kite MCP tool {name!r} failed ({type(exc).__name__}).") from exc

        holdings_result = await call_if_available("get_holdings", required=True)
        positions_result = await call_if_available("get_positions") if include_positions else {"available": False, "reason": "Not requested."}
        margins_result = await call_if_available("get_margins") if include_margins else {"available": False, "reason": "Not requested."}

        holdings_data = holdings_result.get("data")
        holdings = holdings_data if isinstance(holdings_data, list) else None
        if isinstance(holdings_data, dict):
            # Some MCP servers wrap the list in a named response field.
            for key in ("holdings", "data", "items", "results"):
                if isinstance(holdings_data.get(key), list):
                    holdings = holdings_data[key]
                    break

        return {
            "provider": "kite_mcp",
            "source_endpoint": getattr(self.client, "url", None),
            "status": "success",
            "holdings": holdings_data,
            "holdings_count": len(holdings) if holdings is not None else None,
            "positions": positions_result,
            "margins": margins_result,
            "read_only": True,
            "note": "Portfolio values are returned as supplied by Kite MCP; no estimates are substituted.",
        }


def get_portfolio_sync(*, client: Any | None = None, include_positions: bool = True,
                       include_margins: bool = True) -> dict[str, Any]:
    """Synchronous helper for scripts that do not already run an event loop."""
    import asyncio
    return asyncio.run(PortfolioManager(client=client).get_portfolio(
        include_positions=include_positions, include_margins=include_margins))

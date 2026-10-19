"""Minimal lazy MCP SDK client for the official Zerodha Kite MCP server.

No market-data methods are exposed here by design. FYERS remains the market-data
provider; this client is only used for account/order/GTT operations.
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Any


class KiteMCPError(RuntimeError):
    pass


class KiteMCPClient:
    def __init__(self, url: str | None = None, timeout: float = 20.0) -> None:
        self.url = (url or os.getenv("KITE_MCP_URL", "https://mcp.kite.trade/mcp")).strip()
        self.timeout = timeout
        if not self.url.startswith(("https://", "http://")):
            raise ValueError("KITE_MCP_URL must be an http(s) URL")

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover tools advertised by the configured remote MCP server.

        This is diagnostic only: it does not perform a Kite login or submit an
        order. The hosted service may restrict trading tools by policy/account.
        """
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise KiteMCPError("MCP SDK missing. Install project dependencies including 'mcp'.") from exc
        try:
            async with streamablehttp_client(self.url, timeout=timedelta(seconds=self.timeout)) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    available = await session.list_tools()
                    return [
                        {
                            "name": tool.name,
                            "description": getattr(tool, "description", "") or "",
                            "input_schema": getattr(tool, "inputSchema", {}) or {},
                        }
                        for tool in available.tools
                    ]
        except KiteMCPError:
            raise
        except Exception as exc:
            raise KiteMCPError(f"Kite MCP tool discovery failed ({type(exc).__name__}).") from exc

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool using MCP Streamable HTTP; fail closed on tool errors."""
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise KiteMCPError("MCP SDK missing. Install project dependencies including 'mcp'.") from exc

        try:
            async with streamablehttp_client(self.url, timeout=timedelta(seconds=self.timeout)) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    available = await session.list_tools()
                    tool_names = {tool.name for tool in available.tools}
                    if name not in tool_names:
                        raise KiteMCPError(
                            f"Kite MCP tool {name!r} is not available at {self.url}. "
                            "Use an authenticated/self-hosted Kite MCP endpoint with trading tools enabled."
                        )
                    result = await session.call_tool(name, arguments=arguments)
                    if getattr(result, "isError", False):
                        texts = [getattr(part, "text", "") for part in getattr(result, "content", [])]
                        raise KiteMCPError("Kite MCP tool returned an error: " + "; ".join(x for x in texts if x))
                    return {
                        "tool": name,
                        "is_error": False,
                        "content": [
                            {"type": getattr(part, "type", "unknown"), "text": getattr(part, "text", None)}
                            for part in getattr(result, "content", [])
                        ],
                        "structured_content": getattr(result, "structuredContent", None),
                    }
        except KiteMCPError:
            raise
        except Exception as exc:
            raise KiteMCPError(f"Kite MCP request failed ({type(exc).__name__}).") from exc

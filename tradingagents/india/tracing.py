"""Optional LangSmith tracing helpers; application remains usable offline."""
from __future__ import annotations

import os
import importlib.util
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def traced(name: str | None = None) -> Callable[[F], F]:
    """Apply LangSmith traceable when installed; otherwise return function unchanged."""
    def decorate(func: F) -> F:
        if os.getenv("LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING_V2", "false")).lower() not in {"1", "true", "yes"}:
            return func
        try:
            from langsmith import traceable
        except ImportError:
            return func
        return traceable(name=name or func.__name__)(func)  # type: ignore[return-value]
    return decorate


def tracing_status() -> dict[str, Any]:
    enabled = os.getenv("LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING_V2", "false")).lower() in {"1", "true", "yes"}
    sdk_available = importlib.util.find_spec("langsmith") is not None
    key_configured = bool(os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY"))
    return {"enabled": enabled, "project": os.getenv("LANGSMITH_PROJECT", "tradingagents-india"),
            "endpoint_configured": bool(os.getenv("LANGSMITH_ENDPOINT") or os.getenv("LANGCHAIN_ENDPOINT")),
            "api_key_configured": key_configured, "sdk_available": sdk_available,
            "active": enabled and key_configured and sdk_available,
            "note": "Tracing can upload runs to LangSmith when enabled, SDK and credentials are available." if enabled else "Tracing disabled; set LANGSMITH_TRACING=true to enable."}

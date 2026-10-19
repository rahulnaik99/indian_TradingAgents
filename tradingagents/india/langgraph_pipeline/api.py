"""FastAPI adapter for the research-only LangGraph trading analysis pipeline.

The graph is constructed lazily per request so importing this module does not
require provider credentials. Inject ``graph_factory`` and ``AnalysisStore`` in
tests or when embedding the API. No endpoint places broker orders.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tradingagents.india.langgraph_pipeline.analysis_qa import answer_analysis_question
from tradingagents.india.langgraph_pipeline.contracts import validate_request
from tradingagents.india.langgraph_pipeline.graph import create_trading_analysis_graph
from tradingagents.india.langgraph_pipeline.persistence import AnalysisStore


class AnalysisRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=40)
    exchange: str = Field(default="NSE", min_length=1, max_length=20)
    as_of: str = Field(description="ISO-8601 timestamp including timezone")
    timeframes: list[str] = Field(default_factory=lambda: ["1d", "4h", "1h", "15m"], min_length=1)
    analysis_mode: Literal["historical", "latest"] = "historical"
    portfolio: dict[str, Any] | None = None
    risk_limits: dict[str, Any] | None = None

    @field_validator("symbol", "exchange")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class AnalysisResponse(BaseModel):
    analysis_id: str
    status: str
    request: dict[str, Any]
    final_verdict: dict[str, Any]
    errors: list[dict[str, Any]] = Field(default_factory=list)
    workflow_trace: list[str] = Field(default_factory=list)
    live_order_placed: bool = False
    execution_result: dict[str, Any] = Field(default_factory=dict)


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)

    @field_validator("question")
    @classmethod
    def trim_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be empty")
        return value


def create_app(
    *,
    store: AnalysisStore | None = None,
    graph_factory: Callable[..., Any] | None = None,
    graph_options: dict[str, Any] | None = None,
):
    """Create the API application; factories make routes easy to test offline."""
    try:
        from fastapi import FastAPI, HTTPException, Query
    except ImportError as exc:  # pragma: no cover - exercised only without optional deps
        raise RuntimeError("FastAPI is required. Install the API dependencies from pyproject.toml.") from exc

    db = store or AnalysisStore(os.getenv("TRADING_ANALYSIS_DB", "data/trading_analysis.sqlite3"))
    build_graph = graph_factory or create_trading_analysis_graph
    options = dict(graph_options or {})
    app = FastAPI(title="Trading Analysis API", version="1.1.0",
                  description="FYERS-based research and risk analysis. Kite portfolio access and trade execution are temporarily disabled.")
    app.state.analysis_store = db

    @app.get("/health")
    def health() -> dict[str, Any]:
        enabled = os.getenv("PLACE_ORDER", "false").strip().lower() in {"true", "1", "yes"}
        return {"status": "ok", "service": "trading-analysis-api",
                "place_order_flag": enabled, "order_execution_enabled": False,
                "live_gtt_submission_ready": False,
                "portfolio_access_enabled": False,
                "exit_protection_worker_configured": False,
                "market_data_provider": "fyers", "execution_provider": "disabled"}

    @app.get("/kite-mcp/tools")
    async def kite_mcp_tools() -> dict[str, Any]:
        """Diagnostic endpoint: list tools exposed by the configured remote MCP.

        Does not submit orders. Tool discovery is not proof of Kite account login.
        """
        from tradingagents.india.langgraph_pipeline.execution.kite_mcp import KiteMCPClient, KiteMCPError
        try:
            client = KiteMCPClient()
            tools = await client.list_tools()
            return {"endpoint": client.url, "connected": True, "tools": tools,
                    "place_gtt_order_available": any(t.get("name") == "place_gtt_order" for t in tools),
                    "note": "Tool discovery does not verify account authorization or order submission."}
        except KiteMCPError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/portfolio")
    async def get_portfolio() -> dict[str, Any]:
        """Portfolio retrieval is intentionally disabled in the current workflow."""
        raise HTTPException(
            status_code=503,
            detail="Kite MCP portfolio integration is temporarily disabled; analysis does not depend on it.",
        )

    @app.post("/analyses", response_model=AnalysisResponse, status_code=201)
    def create_analysis(body: AnalysisRequestBody) -> dict[str, Any]:
        try:
            request = validate_request(body.model_dump(include={"symbol", "exchange", "as_of", "timeframes", "analysis_mode"}))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        initial: dict[str, Any] = {"request": request, "status": "initialized", "errors": [], "workflow_trace": []}
        if body.portfolio is not None:
            initial["portfolio"] = body.portfolio
        if body.risk_limits is not None:
            initial["risk_limits"] = body.risk_limits
        try:
            graph = build_graph(**options)
            result = graph.invoke(initial)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            # Do not expose provider secrets or stack traces through the API.
            raise HTTPException(status_code=503, detail=f"Analysis pipeline failed: {type(exc).__name__}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("final_verdict"), dict):
            raise HTTPException(status_code=502, detail="Pipeline returned no valid final verdict")
        # Kite MCP portfolio/execution components are bypassed for now.
        # Keep the response contract stable while explicitly reporting that no
        # broker request was attempted. Research and risk analysis still run.
        result["execution_result"] = {
            "status": "EXECUTION_DISABLED",
            "submitted": False,
            "reason": "Kite MCP trade execution is temporarily disabled.",
            "broker": None,
            "market_data_provider": "fyers",
        }
        try:
            analysis_id = db.save_analysis(result)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Analysis completed but persistence failed") from exc
        return {"analysis_id": analysis_id, "status": result.get("status", "partial"),
                "request": result.get("request", request), "final_verdict": result["final_verdict"],
                "errors": result.get("errors", []), "workflow_trace": result.get("workflow_trace", []),
                "live_order_placed": bool((result.get("execution_result") or {}).get("submitted", False)),
                "execution_result": result.get("execution_result", {})}

    @app.get("/analyses")
    def list_analyses(symbol: str | None = None, limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
        return {"items": db.list_analyses(symbol=symbol, limit=limit)}

    @app.get("/analyses/{analysis_id}")
    def get_analysis(analysis_id: str) -> dict[str, Any]:
        result = db.get_analysis(analysis_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Analysis not found")
        return result

    @app.post("/analyses/{analysis_id}/questions")
    def ask_question(analysis_id: str, body: QuestionRequest) -> dict[str, Any]:
        if db.get_analysis(analysis_id) is None:
            raise HTTPException(status_code=404, detail="Analysis not found")
        try:
            return answer_analysis_question(db, analysis_id, body.question)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/analyses/{analysis_id}/questions")
    def list_questions(analysis_id: str, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
        if db.get_analysis(analysis_id) is None:
            raise HTTPException(status_code=404, detail="Analysis not found")
        return {"items": db.list_questions(analysis_id, limit=limit)}

    return app


app = create_app() if os.getenv("TRADING_ANALYSIS_API_AUTOCREATE", "1") == "1" else None

"""Persistent execution audit for complete agent/tool coverage in the UI.

The registry is an explicit inventory of application components. A run snapshots
that inventory and records observed invocations separately from planned/unused
components. Statuses are evidence-based; unknown reasons are never fabricated.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("TRADINGAGENTS_DB_PATH", str(Path(__file__).resolve().parents[2] / "data" / "tradingagents_india.sqlite3")))

# `workflows` defines where a component can participate. The registry intentionally
# includes optional and alternate paths so the UI can show components that did not run.
COMPONENTS: list[dict[str, Any]] = [
    {"id":"technical_analysis", "name":"Technical Analysis", "kind":"agent", "purpose":"Evaluate Daily, 4H and 1H candles and derive indicators/trend context.", "workflows":["integrated_research","technical_analysis","batch_scan"]},
    {"id":"news_research", "name":"News & Policy Research", "kind":"agent", "purpose":"Retrieve company, RBI, government and sector news with source metadata.", "workflows":["integrated_research","news_research"]},
    {"id":"fundamentals_review", "name":"Fundamentals Evidence Review", "kind":"agent", "purpose":"Use stored, sourced financial facts with publication-time controls.", "workflows":["integrated_research","fundamentals"]},
    {"id":"normalize_evidence", "name":"Normalize Point-in-Time Evidence", "kind":"agent", "purpose":"Exclude undated/future events and financial facts and normalize evidence ordering.", "workflows":["integrated_research"]},
    {"id":"risk_assessment", "name":"Deterministic Risk Assessment", "kind":"agent", "purpose":"Evaluate multi-timeframe bias, event risk and fundamental deterioration flags.", "workflows":["integrated_research"]},
    {"id":"synthesis", "name":"Final Evidence Synthesis", "kind":"agent", "purpose":"Produce a bounded evidence summary; optional LLM synthesis can be disabled.", "workflows":["integrated_research"]},
    {"id":"upstream_market_analyst", "name":"Upstream Market Analyst", "kind":"agent", "purpose":"Original Market Analyst; uses the scoped FYERS/shared technical adapter for NSE evidence-package runs.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_news_analyst", "name":"Upstream News Analyst", "kind":"agent", "purpose":"Original News Analyst; company-news tool uses dated shared India events and eligible research citations for NSE evidence-package runs.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_fundamentals_analyst", "name":"Upstream Fundamentals Analyst", "kind":"agent", "purpose":"Original Fundamentals Analyst; statement tools use sourced point-in-time India financial facts for NSE evidence-package runs.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_social_analyst", "name":"Upstream Social Analyst", "kind":"agent", "purpose":"Original Sentiment Analyst; keeps StockTwits/Reddit sentiment sources while company news routes through dated shared India events.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"bull_researcher", "name":"Bullish Researcher", "kind":"agent", "purpose":"Develop the bullish investment thesis in the research debate.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"bear_researcher", "name":"Bearish Researcher", "kind":"agent", "purpose":"Challenge the bullish thesis with bearish evidence.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"research_manager", "name":"Research Manager", "kind":"agent", "purpose":"Synthesize the bull/bear debate into an investment plan.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"trader_agent", "name":"Trader", "kind":"agent", "purpose":"Turn the investment plan into a proposed trade setup.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"aggressive_analyst", "name":"Aggressive Risk Analyst", "kind":"agent", "purpose":"Evaluate the trade proposal from an aggressive risk perspective.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"conservative_analyst", "name":"Conservative Risk Analyst", "kind":"agent", "purpose":"Evaluate the trade proposal from a conservative risk perspective.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"neutral_analyst", "name":"Neutral Risk Analyst", "kind":"agent", "purpose":"Evaluate the trade proposal from a neutral risk perspective.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"portfolio_manager", "name":"Hedge Fund / Portfolio Manager", "kind":"agent", "purpose":"Synthesize research, trade and risk debate into the final portfolio decision.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_stock_data", "name":"Tool · get_stock_data", "kind":"tool", "purpose":"Retrieve FYERS daily OHLCV for the original Market Analyst during India evidence-package runs; fail closed instead of silently falling back.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_indicators", "name":"Tool · get_indicators", "kind":"tool", "purpose":"Read selected indicators from the shared India technical snapshot during India evidence-package runs.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_verified_market_snapshot", "name":"Tool · get_verified_market_snapshot", "kind":"tool", "purpose":"Verify the current market snapshot used in the upstream market report.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_fundamentals", "name":"Tool · get_fundamentals", "kind":"tool", "purpose":"Read sourced, point-in-time Indian financial facts for the original Fundamentals Analyst.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_balance_sheet", "name":"Tool · get_balance_sheet", "kind":"tool", "purpose":"Fetch balance-sheet statements.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_cashflow", "name":"Tool · get_cashflow", "kind":"tool", "purpose":"Fetch cash-flow statements.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_income_statement", "name":"Tool · get_income_statement", "kind":"tool", "purpose":"Fetch income statements.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_news", "name":"Tool · get_news", "kind":"tool", "purpose":"Read timestamp-filtered India events and eligible research citations for the original News Analyst.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_global_news", "name":"Tool · get_global_news", "kind":"tool", "purpose":"Fetch macro and global news.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_insider_transactions", "name":"Tool · get_insider_transactions", "kind":"tool", "purpose":"Fetch insider transaction data.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_macro_indicators", "name":"Tool · get_macro_indicators", "kind":"tool", "purpose":"Fetch macroeconomic indicators.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_tool_get_prediction_markets", "name":"Tool · get_prediction_markets", "kind":"tool", "purpose":"Fetch prediction-market context when selected by an analyst.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"upstream_other_tool", "name":"Other upstream tool", "kind":"tool", "purpose":"Catch-all audit row for a tool name not mapped to the known upstream tool registry.", "workflows":["upstream_agent_graph","integrated_research"]},
    {"id":"fyers_daily_history", "name":"FYERS Daily History", "kind":"tool", "purpose":"Fetch daily historical OHLCV candles.", "workflows":["integrated_research","technical_analysis","batch_scan"]},
    {"id":"fyers_hourly_history", "name":"FYERS Hourly History", "kind":"tool", "purpose":"Fetch hourly candles used for 4H aggregation and 1H analysis.", "workflows":["integrated_research","technical_analysis","batch_scan"]},
    {"id":"fyers_15m_history", "name":"FYERS 15m History", "kind":"tool", "purpose":"Optional lower-timeframe refinement.", "workflows":["integrated_research","technical_analysis","batch_scan"]},
    {"id":"indicator_calculation", "name":"Indicator Calculation", "kind":"tool", "purpose":"Calculate trend and technical indicator outputs from fetched candles.", "workflows":["integrated_research","technical_analysis","batch_scan"]},
    {"id":"evidence_package", "name":"Shared Point-in-Time Evidence Package", "kind":"tool", "purpose":"Validate publication timestamps and provide a bounded, sourced evidence snapshot to both India research and original TradingAgents agents.", "workflows":["integrated_research"]},
    {"id":"wigolo_search", "name":"Wigolo Search", "kind":"tool", "purpose":"Search current web sources when configured and reachable.", "workflows":["integrated_research","news_research"]},
    {"id":"wigolo_research", "name":"Wigolo Deep Research", "kind":"tool", "purpose":"Run multi-step web research with source cross-checking and a structured evidence brief.", "workflows":["integrated_research","news_research"]},
    {"id":"google_news_rss", "name":"Google News RSS Fallback", "kind":"tool", "purpose":"Fallback news search when Wigolo search is unavailable.", "workflows":["integrated_research","news_research"]},
    {"id":"event_store_write", "name":"Sourced Event Store Write", "kind":"tool", "purpose":"Persist retrieved news/events with source and timestamp metadata.", "workflows":["integrated_research","news_research"]},
    {"id":"event_store_read", "name":"Sourced Event Store Read", "kind":"tool", "purpose":"Load saved events for point-in-time review.", "workflows":["integrated_research"]},
    {"id":"calendar_store_read", "name":"NSE Events Calendar Read", "kind":"tool", "purpose":"Load relevant scheduled NSE corporate/macro events and filter them by when the app first knew about them.", "workflows":["integrated_research"]},
    {"id":"fundamentals_store_read", "name":"Financial Facts Store Read", "kind":"tool", "purpose":"Load stored financial facts and build point-in-time summaries.", "workflows":["integrated_research","fundamentals"]},
    {"id":"xbrl_parser", "name":"XBRL Parser", "kind":"tool", "purpose":"Parse supplied XBRL filing content into sourced facts; used only during import/parse requests.", "workflows":["fundamentals"]},
    {"id":"llm_synthesis", "name":"LLM Synthesis", "kind":"tool", "purpose":"Optional configured LLM summary over supplied evidence.", "workflows":["integrated_research"]},
    {"id":"langsmith_tracing", "name":"LangSmith Tracing", "kind":"tool", "purpose":"Optional remote observability; only active with SDK, key and tracing enabled.", "workflows":["integrated_research","upstream_agent_graph","technical_analysis","batch_scan","news_research","fundamentals"]},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS execution_runs (
      run_id TEXT PRIMARY KEY, workflow TEXT NOT NULL, symbol TEXT, status TEXT NOT NULL,
      started_at TEXT NOT NULL, finished_at TEXT, params_json TEXT NOT NULL, result_summary_json TEXT
    );
    CREATE TABLE IF NOT EXISTS execution_components (
      run_id TEXT NOT NULL, component_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
      purpose TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, invocation_count INTEGER NOT NULL DEFAULT 0,
      started_at TEXT, finished_at TEXT, details_json TEXT NOT NULL DEFAULT '{}',
      PRIMARY KEY(run_id, component_id), FOREIGN KEY(run_id) REFERENCES execution_runs(run_id)
    );
    CREATE TABLE IF NOT EXISTS execution_events (
      event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, component_id TEXT,
      event_type TEXT NOT NULL, timestamp TEXT NOT NULL, message TEXT, details_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_execution_events_run ON execution_events(run_id, event_id);
    """)
    return conn


def create_run(workflow: str, symbol: str | None = None, params: dict[str, Any] | None = None) -> str:
    run_id = uuid.uuid4().hex
    started = _now()
    with _connect() as c:
        c.execute("INSERT INTO execution_runs(run_id,workflow,symbol,status,started_at,params_json) VALUES(?,?,?,?,?,?)",
                  (run_id, workflow, symbol, "running", started, json.dumps(params or {}, default=str)))
        for comp in COMPONENTS:
            status = "not_invoked" if workflow in comp["workflows"] else "not_applicable"
            reason = None if status == "not_invoked" else f"Component is registered for other workflow(s): {', '.join(comp['workflows'])}."
            c.execute("INSERT INTO execution_components(run_id,component_id,name,kind,purpose,status,reason) VALUES(?,?,?,?,?,?,?)",
                      (run_id, comp["id"], comp["name"], comp["kind"], comp["purpose"], status, reason))
        c.execute("INSERT INTO execution_events(run_id,event_type,timestamp,message,details_json) VALUES(?,?,?,?,?)",
                  (run_id, "run_started", started, f"Started {workflow} for {symbol or 'no symbol'}", json.dumps(params or {}, default=str)))
    return run_id


def mark_component(run_id: str, component_id: str, status: str, *, reason: str | None = None,
                   details: dict[str, Any] | None = None, event_type: str | None = None, message: str | None = None) -> None:
    allowed = {"running", "completed", "failed", "skipped", "not_invoked", "not_reached", "unavailable", "unknown", "not_applicable"}
    if status not in allowed:
        raise ValueError(f"Invalid execution component status: {status}")
    timestamp = _now()
    details = details or {}
    with _connect() as c:
        row = c.execute("SELECT invocation_count,started_at,status FROM execution_components WHERE run_id=? AND component_id=?", (run_id, component_id)).fetchone()
        if row is None:
            return
        invocation_count = int(row["invocation_count"])
        if status in {"running", "completed", "failed"} and event_type != "availability":
            if status == "running":
                invocation_count += 1
            elif event_type == "agent_state_observed" and row["status"] in {"not_invoked", "unknown"}:
                # Some upstream LangGraph node outputs are only exposed in final state.
                # Count these as observed invocations without inventing a start timestamp.
                invocation_count += 1
        started_at = row["started_at"] or (timestamp if status == "running" else None)
        finished_at = timestamp if status in {"completed", "failed", "skipped", "not_reached", "unavailable", "unknown", "not_applicable", "not_invoked"} else None
        c.execute("""UPDATE execution_components SET status=?,reason=COALESCE(?,reason),invocation_count=?,
                     started_at=COALESCE(started_at,?),finished_at=COALESCE(?,finished_at),details_json=?
                     WHERE run_id=? AND component_id=?""",
                  (status, reason, invocation_count, started_at, finished_at, json.dumps(details, default=str), run_id, component_id))
        c.execute("INSERT INTO execution_events(run_id,component_id,event_type,timestamp,message,details_json) VALUES(?,?,?,?,?,?)",
                  (run_id, component_id, event_type or status, timestamp, message or reason or status, json.dumps(details, default=str)))


def add_event(run_id: str, event_type: str, message: str, *, component_id: str | None = None,
              details: dict[str, Any] | None = None) -> None:
    with _connect() as c:
        c.execute("INSERT INTO execution_events(run_id,component_id,event_type,timestamp,message,details_json) VALUES(?,?,?,?,?,?)",
                  (run_id, component_id, event_type, _now(), message, json.dumps(details or {}, default=str)))


def finish_run(run_id: str, status: str, summary: dict[str, Any] | None = None) -> None:
    timestamp = _now()
    with _connect() as c:
        c.execute("UPDATE execution_runs SET status=?,finished_at=?,result_summary_json=? WHERE run_id=?",
                  (status, timestamp, json.dumps(summary or {}, default=str), run_id))
        # Components still marked running are never silently presented as completed.
        if status == "failed":
            c.execute("UPDATE execution_components SET status='failed',reason=COALESCE(reason,'Run failed before a completion event was recorded.'),finished_at=? WHERE run_id=? AND status='running'", (timestamp, run_id))
            c.execute("UPDATE execution_components SET status='not_reached',reason=COALESCE(reason,'Run failed before this component was reached.'),finished_at=? WHERE run_id=? AND status='not_invoked'", (timestamp, run_id))
        else:
            c.execute("UPDATE execution_components SET status='not_reached',reason=COALESCE(reason,'Run ended before a completion event was recorded.'),finished_at=? WHERE run_id=? AND status='running'", (timestamp, run_id))
        c.execute("INSERT INTO execution_events(run_id,event_type,timestamp,message,details_json) VALUES(?,?,?,?,?)",
                  (run_id, "run_finished", timestamp, f"Run finished with status {status}", json.dumps(summary or {}, default=str)))


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    with _connect() as c:
        rows = c.execute("SELECT run_id,workflow,symbol,status,started_at,finished_at,params_json,result_summary_json FROM execution_runs ORDER BY started_at DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
    return [{**dict(r), "params": json.loads(r["params_json"]), "result_summary": json.loads(r["result_summary_json"] or "{}"), "params_json": None, "result_summary_json": None} for r in rows]


def get_run(run_id: str) -> dict[str, Any] | None:
    with _connect() as c:
        run = c.execute("SELECT * FROM execution_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            return None
        components = c.execute("SELECT * FROM execution_components WHERE run_id=? ORDER BY kind,name", (run_id,)).fetchall()
        events = c.execute("SELECT * FROM execution_events WHERE run_id=? ORDER BY event_id", (run_id,)).fetchall()
    run_data = dict(run)
    run_data["params"] = json.loads(run_data.pop("params_json"))
    run_data["result_summary"] = json.loads(run_data.pop("result_summary_json") or "{}")
    comps = []
    for row in components:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json") or "{}")
        comps.append(item)
    evs = []
    for row in events:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json") or "{}")
        evs.append(item)
    return {**run_data, "components": comps, "events": evs}

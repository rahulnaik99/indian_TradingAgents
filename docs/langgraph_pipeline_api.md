# LangGraph Trading Analysis API

Research-only FastAPI wrapper around `tradingagents.india.langgraph_pipeline`. It does not submit or execute broker orders.

## Install and run

From the repository root, use the project environment and install project dependencies:

```bash
python -m pip install -e .
uvicorn tradingagents.india.langgraph_pipeline.api_main:app --host 127.0.0.1 --port 8000
```

Set `TRADING_ANALYSIS_DB` to change the SQLite file location. The default is `data/trading_analysis.sqlite3`. The first request that submits an analysis compiles the LangGraph graph, so LangGraph and provider dependencies must be installed. Configure the data providers according to the upstream project before expecting live market evidence.

## Endpoints

- `GET /health` — health status; portfolio access and order execution are disabled.
- `POST /analyses` — validate request, execute graph, and persist the result.
- `GET /analyses?symbol=RELIANCE&limit=20` — list recent analysis metadata.
- `GET /analyses/{analysis_id}` — retrieve the full saved snapshot.
- `POST /analyses/{analysis_id}/questions` — answer from the saved analysis and persist the Q&A.
- `GET /analyses/{analysis_id}/questions` — retrieve saved Q&A history.
- `GET /portfolio` — returns `503` while Kite MCP portfolio integration is disabled.

### Request example

```json
{
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "as_of": "2026-10-16T15:30:00+05:30",
  "timeframes": ["1d", "4h", "1h", "15m"],
  "analysis_mode": "historical"
}
```

`as_of` must be an ISO-8601 timestamp with timezone. `timeframes` accepts `1d`, `4h`, `1h`, and `15m`. Optional `portfolio` and `risk_limits` objects can be provided to enable applicable risk checks; missing values are not inferred.

### Question example

```json
{"question": "Why did this analysis conclude no trade?"}
```

The current Q&A uses the persisted snapshot. It does not call an LLM by default. Additional research is not automatically enabled by this API endpoint.

## Safety and limitations

- The API never places orders; all responses set `live_order_placed` to `false`.
- A successful HTTP response means the graph returned a structured verdict and the snapshot was persisted, not that a trade is suitable or profitable.
- The API does not currently authenticate callers. Bind to localhost for development; add authentication, authorization, TLS, rate limiting, and deployment secrets management before exposing it on a network.
- Graph execution is synchronous in this initial API. Long-running provider calls can hold the request open; production deployments should use a job queue and status polling.
- Provider availability, data-source credentials, and compiled LangGraph runtime still require environment-specific end-to-end validation.

## Kite MCP portfolio and execution (temporarily disabled)

The active analysis workflow does not call the Kite MCP Portfolio Manager or
trade-execution boundary. It continues through the existing FYERS-backed
research, trader proposal, risk review, and final research verdict nodes.

- `GET /portfolio` returns `503` with an explicit disabled message.
- `POST /analyses` never submits orders and returns
  `execution_result.status = "EXECUTION_DISABLED"` and `submitted = false`.
- `/health` reports `portfolio_access_enabled = false`,
  `order_execution_enabled = false`, and `live_gtt_submission_ready = false`,
  regardless of `PLACE_ORDER` configuration.
- `GET /kite-mcp/tools` remains available as a standalone, read-only diagnostic;
  it is not called by the active analysis flow.
- FYERS remains the configured market-data provider. The research Trader still
  creates a proposal, and existing risk checks still run. A proposal is not an
  order and is not represented as an executed trade.

The Kite MCP client, portfolio module, and GTT executor files are retained for
future re-enablement; they are simply bypassed by the active API workflow.

## Code organization

Agent implementations are grouped under `tradingagents/india/langgraph_pipeline/agents/`, with one folder per agent. The graph, state, data collection, persistence, and API modules stay in their existing locations to avoid unnecessary import churn. Shared LLM clients, settings, and logging remain in their existing project-wide modules.

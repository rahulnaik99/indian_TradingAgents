# TradingAgents India — Implementation Status

**Build date:** 10 October 2026  
**Scope:** User-requested India-market adaptation for swing trading, all current NIFTY 50 constituents initially, with later NSE-wide expansion.  
**Status:** Broad feature implementation delivered as a development package. Not production-validated.

## Features now present in code

### Market and technical analysis
- Configurable dated NIFTY 50 registry and NSE calendar foundation.
- FYERS v3 historical OHLCV adapter with rate limiting, retry behavior, response validation and session-aware 4H aggregation.
- Daily + 4H + 1H swing-analysis core; optional 15m refinement; 1m excluded from primary swing strategy.
- EMA 20/50/100/200, RSI, ATR, session VWAP where applicable, support/resistance, previous candle high/low, relative volume, market-structure approximation, Fibonacci retracement levels, and illustrative stop/target levels.
- User-triggered NIFTY 50 background batch scan with job status polling and a shared market-data client/limiter.

### News and policy research
- Wigolo REST integration for `/v1/search`, `/v1/fetch`, `/v1/research` and `/health`.
- Google News RSS fallback for search when Wigolo is not running.
- Preliminary rule-based classification for RBI monetary policy, regulatory action, quarterly results, index changes, government policy and corporate actions.
- Event persistence with source URL, publication timestamp where available, retrieval time, severity, direction and confidence.
- Stock-specific news endpoint for each configured NIFTY 50 symbol.

### Fundamentals
- NSE / company-IR / RBI source guidance.
- Point-in-time CSV financial-fact import.
- Raw XBRL fact extraction preserving original taxonomy labels and context periods.
- QoQ/YoY summary computed only from matching metric, unit, scope and period type; publication-time filtering and revision selection.
- Moneycontrol table fetcher gated by `MONEYCONTROL_PERMISSION_CONFIRMED=true`; default is blocked. The adapter returns raw table rows, not a fully normalized P&L/balance-sheet dataset.

### Workflow, observability and UI
- Integrated LangGraph workflow combines the deterministic technical report, saved/just-fetched news/events and imported financial facts.
- Future and undated evidence is excluded from the point-in-time integrated evidence set.
- Deterministic event/fundamental risk flags precede optional LLM synthesis.
- Existing upstream TradingAgents graph can be invoked separately with `ENABLE_LANGGRAPH_AGENT_ANALYSIS=true`; that upstream graph still uses its original dataflows rather than directly consuming the new FYERS and local event/fundamental stores.
- Optional LangSmith tracing through environment configuration and custom trace spans.
- FastAPI-served dashboard includes universe, technical analysis, batch scan, news/research, fundamentals, paper trading, integrated workflow and gated live-order controls.

### Order execution
- Paper trading with simulated fills, long-only position tracking and realized P&L before costs.
- FYERS live LIMIT/CNC order placement, modification, cancellation and orderbook adapter.
- Live adapter is disabled by default and requires all of:
  - `ENABLE_LIVE_TRADING=true`
  - `LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_MONEY_RISK`
  - per-request `confirm_live_order=PLACE_REAL_ORDER`
- Quantity and notional limits are configurable. Write requests are not retried after ambiguous network failures.
- **Important:** live adapter does not automatically submit linked stop-loss/target protection and is not production-ready for unattended trading. It has not been tested against real credentials.

## Validation performed

- **73 targeted tests passed** across the India contracts/calendar/universe, FYERS historical adapter, technical indicators, news classification/RSS fallback, financial CSV/XBRL, Moneycontrol permission gate, point-in-time workflow, paper trading, scan job lifecycle, live-order safety gates and dashboard routes.
- Python compile checks passed.
- `bash -n start.sh` passed.
- Embedded dashboard JavaScript passed `node --check`.
- Local FastAPI smoke test passed: health endpoint, 50-member universe, dashboard served, and live order endpoint returned HTTP 403 by default.
- ZIP integrity check is performed after packaging.

## Not verified / outstanding

1. No real FYERS market-data or live-order request was made; account credentials and permissions are not configured in this environment.
2. No LangSmith trace was uploaded; no LangSmith key is configured.
3. No live Wigolo daemon was running during tests. The REST adapter is integrated and the RSS fallback is tested, but actual Wigolo search/fetch/research calls need a local smoke test.
4. No Moneycontrol collection was performed. Written permission/licensing is required before automated use; the parser returns raw table rows and does not guarantee normalized statements.
5. Official NSE XBRL taxonomy mappings, units/decimals and sector-specific financial metric normalization need completion and reconciliation against issuer filings.
6. The NIFTY 50 registry is a dated snapshot; live index membership and a complete historical constituent archive are not provided.
7. NSE calendar data is limited to the configured year(s) in the checked-in data; keep calendars maintained and validate special sessions.
8. 4H aggregation is session-aware but must be validated against a trusted reference set of real provider candles before production backtesting.
9. The existing full upstream test suite could not initialize because `tests/conftest.py` imports `yfinance`, which is missing from the available environment. The 72 passing tests are the isolated targeted suite, not the entire upstream suite.
10. Batch scans are user-triggered. A 50-stock scan uses roughly 100 history requests (plus optional 15m requests), so a full scan should not run every five minutes on Standard FYERS quotas. Use streaming/incremental monitoring for high-frequency refresh.
11. Live execution still needs protective stop-loss/target lifecycle, tick-size/symbol-master validation, portfolio risk/exposure checks, full order-state reconciliation and broker-side integration tests.
12. The app binds to localhost by default and has no user authentication. Do not expose it publicly without a security layer.

## Relevant environment settings

- FYERS data: `FYERS_APP_ID`, `FYERS_ACCESS_TOKEN`, `FYERS_REQUESTS_PER_SECOND`, `FYERS_REQUESTS_PER_MINUTE`, `FYERS_REQUESTS_PER_DAY`.
- Wigolo: `WIGOLO_BASE_URL`, optional `WIGOLO_API_TOKEN`.
- LangSmith: `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`, `LANGSMITH_ENDPOINT`.
- Integrated LLM synthesis: `ENABLE_INDIA_LLM_SYNTHESIS=true` (optional; may incur model costs).
- Existing upstream graph: `ENABLE_LANGGRAPH_AGENT_ANALYSIS=true` (optional; may incur model/data-provider costs).
- Moneycontrol: `MONEYCONTROL_PERMISSION_CONFIRMED=true` only after written permission/licensing.
- Live order adapter: `ENABLE_LIVE_TRADING`, `LIVE_TRADING_CONFIRMATION`, `LIVE_MAX_ORDER_QTY`, `LIVE_MAX_ORDER_VALUE_INR` (disabled by default).

## Important APIs

- `GET /api/universe`
- `GET /api/fyers/history`
- `GET /api/analysis/{symbol}`
- `POST /api/scan` / `GET /api/scan/{job_id}`
- `POST /api/research/{symbol}` — integrated LangGraph workflow
- `POST /api/agent-analysis/{symbol}` — existing upstream graph, opt-in
- `GET /api/news/search`, `/api/news/stock/{symbol}`, `/api/news/research`, `/api/web/fetch`
- `POST /api/fundamentals/import-csv`, `POST /api/fundamentals/xbrl/parse`, `GET /api/fundamentals/{symbol}/summary`
- `POST /api/paper/orders`, `GET /api/paper/portfolio`
- `POST /api/live/orders`, `PATCH /api/live/orders/{order_id}`, `DELETE /api/live/orders/{order_id}` — disabled by default

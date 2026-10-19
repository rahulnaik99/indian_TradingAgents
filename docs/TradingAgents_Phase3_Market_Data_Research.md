# TradingAgents --- Phase 3: Indian Market Data Source Research

**Status:** Phase 3 research complete --- awaiting approval before Phase
4.\
**Research date:** 9 October 2026\
**Scope:** Market-price data sources, historical candles, live data,
limits, pricing, MCP availability, and suitability for the approved
swing-trading design.\
**No application code was changed.**

## 1. Confirmed swing-trading requirements

The approved technical-analysis design is:

-   **Daily (1D):** primary trend and major support/resistance.
-   **4-hour (4H):** intermediate trend and pullback structure.
-   **Hourly (1H):** setup confirmation.
-   **15-minute (15m):** optional entry refinement.
-   **1-minute:** excluded from primary swing analysis; existing
    1-minute history may be retained for research, but should not drive
    the initial strategy.
-   Use closed candles for confirmed signals, `Asia/Kolkata` timestamps,
    session-aware aggregation, and point-in-time protection.
-   Live order placement remains disabled by default.

For this strategy, reliable daily and hourly history matters more than
ultra-low-latency tick data.

## 2. Provider comparison

  ----------------------------------------------------------------------------------------------------------------------------
  Source        Historical      Live market    Published price / limits  MCP availability               Assessment
                OHLCV           data                                                                    
  ------------- --------------- -------------- ------------------------- ------------------------------ ----------------------
  **FYERS API   Official        Streaming data Official FYERS support    **Official FYERS MCP exists.** **Preferred first
  v3**          documentation   sockets;       page lists Standard Data  Official page lists OAuth2     adapter candidate**,
                supports        Standard and   API limits of 5 req/s,    authorization and MCP endpoint especially because the
                historical      Prime have     50/min, 5,000/day; Prime  `https://mcp.fyers.in/mcp`; it project already has
                candles. FYERS  different      10 req/s, 500/min,        exposes account/market-data    FYERS API experience.
                says Standard   symbol/rate    500,000/day. FYERS says   workflows. Official FYERS      Use direct
                has up to 5     limits.        Trading API and           support describes its MCP as   REST/WebSocket API for
                years of daily                 market-data access are    read-only, while the newer     deterministic data
                history and 1                  free, while               product page mentions trading  ingestion; MCP is
                year of                        feature/history           workflows; treat capabilities  optional for AI tool
                intraday                       availability depends on   as                             access, not the
                history; Prime                 plan.                     version/permission-dependent   canonical
                expands this to                                          and verify before use.         historical-data
                20+ years daily                                                                         engine.
                and 5+ years                                                                            
                intraday.                                                                               

  **Zerodha     Official candle WebSocket      Official pricing:         **Official Zerodha Kite MCP    **Secondary/fallback
  Kite          API supports    market data.   Personal free tier        server exists**, hosted at     provider candidate.**
  Connect**     minute, 3m, 5m,                excludes live/historical  `https://mcp.kite.trade/mcp`   Good documented candle
                10m, 15m, 30m,                 data; Connect costs       and available for              intervals and a
                60m and daily                  ₹500/month per API key    self-hosting. Hosted mode      first-party MCP
                intervals.                     and includes both.        excludes destructive trading   implementation, but
                                               Historical data is        operations.                    costs ₹500/month per
                                               rate-limited; official                                   API key for data.
                                               exception docs should be                                 
                                               rechecked during                                         
                                               implementation.                                          

  **NSE         Product         Official       Commercial terms,         No general public NSE          Use official NSE
  official data availability    real-time and  permitted use and         market-data MCP was verified   products and filings
  products**    depends on      snapshot       delivery method depend on in this research.              where needed for
                subscription    products are   the                                                      authoritative
                and product     available to   subscription/agreement.                                  data/licensing. Not
                agreement. NSE  subscribers.                                                            the default retail
                offers                                                                                  OHLCV adapter until
                market-data                                                                             requirements and
                products                                                                                pricing are confirmed.
                including                                                                               
                real-time                                                                               
                stock-wise                                                                              
                feed, snapshot                                                                          
                data and                                                                                
                delayed                                                                                 
                snapshots. Do                                                                           
                not assume the                                                                          
                public website                                                                          
                is a free,                                                                              
                stable                                                                                  
                historical API.                                                                         

  **Public      Varies;         May be         Terms and rate limits     Third-party MCP projects       Use for discovery or
  website       completeness,   delayed,       vary; website scraping    exist, but they are not        corroboration only,
  scraping /    historical      cached or      can break without notice. equivalent to official         not as the source of
  unofficial    coverage,       blocked.                                 exchange or broker guarantees. truth for backtests or
  market-data   schema                                                                                  trade-critical
  MCPs**        stability and                                                                           candles.
                adjustments may                                                                         
                be unclear.                                                                             
  ----------------------------------------------------------------------------------------------------------------------------

### Primary sources

-   FYERS API rate limits, history coverage and socket limits:\
    https://support.fyers.in/portal/en/kb/articles/what-are-the-fyers-api-rate-limits-for-standard-and-prime-plans
-   FYERS API pricing statement:\
    https://support.fyers.in/portal/en/kb/articles/does-fyers-charge-any-subscription-fees-for-trading-api
-   Official FYERS MCP:\
    https://fyers.in/mcp
-   FYERS MCP connections and stated read-only behavior:\
    https://support.fyers.in/portal/en/kb/articles/what-connections-are-available-with-fyers-mcp
-   Kite Connect pricing:\
    https://www.zerodha.com/products/api/
-   Kite Connect API pricing support article:\
    https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/what-are-the-charges-for-kite-apis
-   Kite historical candle API:\
    https://kite.trade/docs/connect/v3/historical/
-   Official Zerodha Kite MCP server:\
    https://github.com/zerodha/kite-mcp-server
-   NSE real-time and snapshot data products:\
    https://www.nseindia.com/static/market-data/real-time-data-subscription
-   NSE data usage policy:\
    https://www.nseindia.com/static/market-data/nse-data-policy
-   NSE market timings and holidays:\
    https://www.nseindia.com/resources/exchange-communication-holidays

## 3. What this means for swing trading

### Daily candles

FYERS Standard's stated 5-year daily coverage should usually be
sufficient to start validating swing rules over multiple market regimes.
If the strategy needs longer history, verify FYERS Prime coverage or
another properly licensed source.

### 4-hour candles

Kite's published native intervals do not include 4-hour candles.
Construct 4H candles from validated lower-timeframe data using
exchange-session-aware boundaries. The FYERS API's current resolution
options should be checked directly in the implementation spike; if
native 4H is unavailable, aggregate from hourly candles or another
sufficiently granular source.

### Hourly and 15-minute candles

Kite explicitly documents 60-minute and 15-minute candles. FYERS history
supports intraday data, but exact supported resolution values and
maximum request windows should be verified against the current API
documentation before coding.

### Closed-candle policy

Broker APIs can return a currently forming candle. The ingestion layer
must distinguish completed candles from in-progress candles. For
confirmed swing signals, only candles whose close time has passed should
be used. Record source timestamp, retrieval timestamp, interval and
finality status.

## 4. Recommended data architecture

``` text
FYERS REST / WebSocket (primary candidate)     Kite Connect (optional fallback)
                  |                                          |
                  +-------------------+----------------------+
                                      |
                         Provider-specific adapters
                                      |
                       Rate limiting + retries + caching
                                      |
                    Canonical instrument / symbol mapping
                                      |
                       OHLCV validation and normalization
                                      |
                         Session-aware candle builder
                       (1D / 4H / 1H / optional 15m)
                                      |
                     Persistent historical candle store
                                      |
                  Deterministic indicators and backtesting
                                      |
                          TradingAgents / LangGraph
```

### Canonical candle fields

Each candle should retain:

-   `symbol`, `exchange`, `instrument_id`, `timeframe`
-   `open_time`, `close_time`, `timezone`
-   `open`, `high`, `low`, `close`, `volume`
-   `source_provider`, `retrieved_at`
-   `is_complete`, `is_adjusted` (when known), `quality_flags`
-   optional source payload hash / raw response reference for
    auditability

Do not silently blend providers into one series without reconciling
timestamps, corporate-action adjustments, symbol mapping, and data gaps.
Keep source provenance for every record.

## 5. Data quality and backtesting safeguards

1.  **No look-ahead bias:** historical decisions can use only candles
    and disclosures available at that timestamp.
2.  **Exchange sessions:** use NSE/BSE calendars, holidays, special
    sessions and actual session boundaries; do not fabricate candles
    during closures.
3.  **Corporate actions:** define how splits, bonuses, dividends and
    symbol changes are represented. Preserve raw data and adjustment
    metadata.
4.  **Missing / duplicate candles:** validate ordering, duplicates, OHLC
    invariants, volume and session gaps; do not fill missing candles
    with synthetic prices without an explicit policy.
5.  **Provider outage:** cache previously validated history, mark stale
    live data, and stop analysis if required data is missing or stale.
6.  **Rate limits:** use provider-specific token buckets, exponential
    backoff for transient errors, and caching. Do not retry
    authentication or permission errors indefinitely.
7.  **No live orders:** data adapters must be read-only for this phase;
    trading execution remains disabled.
8.  **Data licensing:** use broker API terms for intended use and verify
    NSE redistribution/display restrictions before exposing data to
    other users or hosting publicly.

## 6. Recommendation

**Provisional recommendation: implement FYERS as the first direct
market-data adapter; keep Kite Connect as an optional secondary
adapter.**

Rationale: - The project already has FYERS integration experience. -
FYERS officially documents multi-year daily data and intraday coverage,
plus market-data sockets. - FYERS currently states that its Trading API
and market-data access are free, but plan-dependent limits and history
coverage must be checked for the user's account. - Kite is a
well-documented alternative, but real-time and historical data cost
₹500/month per API key. - Use official NSE products when the required
data or licensing calls for them; do not rely on scraping NSE pages as a
substitute for an authorized market-data feed.

**Important:** this is a source-research recommendation, not
confirmation that credentials, endpoints, permissions or live data have
been tested in the repository. Those checks belong in the approved
implementation phase.

## 7. Decisions requested before Phase 4

Please approve or revise these decisions:

1.  FYERS direct API as the first market-data adapter.
2.  Kite Connect as optional fallback, only if credentials/subscription
    are available.
3.  Daily + session-aware 4H + 1H as core swing timeframes; 15m
    optional.
4.  Read-only market-data integration; no live order placement.
5.  Proceed to Phase 4 for fundamental-data source research (NSE
    filings, company investor relations, financial-statement sources,
    and whether the intended third-party website is Moneycontrol or
    Moneyview).

**Phase 3 status: complete. Waiting for approval before Phase 4.**

# TradingAgents India --- Proposed Architecture & Workflow

**Purpose:** Decision document for reviewing the target architecture
before implementation.\
**Project scope:** Swing-trading research for all current NIFTY 50
constituents first, with a design that can expand to NIFTY 100, NIFTY
500 and eventually eligible NSE-listed equities.\
**Status:** Proposed design --- for review and decisions. Not
implemented.\
**Date:** 9 October 2026

------------------------------------------------------------------------

## 1. Executive summary

The recommended design preserves the existing TradingAgents LangGraph
workflow and adds India-specific services around it instead of rewriting
the agent framework.

The system will combine:

1.  **Market data** from approved broker/data-provider adapters.
2.  **Web intelligence** for company news, RBI policy, government/sector
    policy, corporate disclosures and index changes.
3.  **Fundamental data** from official NSE filings/XBRL and company
    investor-relations documents, with Moneycontrol only if
    automated-use permission/licensing is obtained.
4.  **Deterministic analytics** for candle construction, indicators,
    financial ratios, event timestamps, risk arithmetic and backtesting.
5.  **LLM agents** to summarize evidence, debate interpretations,
    explain risks and produce a structured research report.
6.  **A UI** to view universe coverage, workflow state, stock analysis,
    source evidence, event impacts and backtest results.
7.  **LangSmith tracing** to inspect agent execution, latency, errors
    and token/cost usage.

The application is research-first. **Live order placement is disabled by
default.** Paper trading, if desired, should be a separate explicitly
approved phase.

## 2. High-level architecture

``` text
                               USER / SCHEDULE
                                      |
                                      v
                          Web UI / API / CLI entry
                                      |
                                      v
                         Analysis & Workflow Orchestrator
                           (existing LangGraph graph)
                                      |
             +------------------------+------------------------+
             |                        |                        |
             v                        v                        v
       Technical Analyst       News / Event Analyst      Fundamentals Analyst
             |                        |                        |
             +------------------------+------------------------+
                                      |
                                      v
                          Research / Debate / Risk Agents
                                      |
                                      v
                         Portfolio / Final Report Agent
                                      |
                                      v
                         Structured Research Result + UI


     DATA SERVICES (independent of LLM reasoning)
     ================================================================

  Market Data Adapters         Web Intelligence             Financial Data
  --------------------         ----------------             --------------
  FYERS (primary candidate)    Wigolo MCP (candidate)       NSE filings / XBRL
  Kite (optional fallback)     RSS / authorized sources     Company IR websites
  Licensed feed if needed      RBI / Government sites       Licensed vendor if needed
           |                            |                            |
           +----------------------------+----------------------------+
                                        |
                                        v
                            Normalize / Validate / Timestamp
                                        |
                                        v
                          Point-in-Time Data & Event Store
                                        |
                  +---------------------+---------------------+
                  |                     |                     |
                  v                     v                     v
             OHLCV candles         News / policy events   Financial facts
                  |                     |                     |
                  v                     v                     v
            Indicators & MTF       Event impact engine   Ratios / fundamentals
                  +---------------------+---------------------+
                                        |
                                        v
                              TradingAgents LangGraph
```

## 3. Stock universe: NIFTY 50 first, NSE-wide later

### Initial universe

-   All current NIFTY 50 constituents.
-   Maintain sector and industry classification where available.
-   Include index-level context such as NIFTY 50 and relevant sector
    indices where data access permits.
-   CANARABANK is an example, not a hard-coded special case.

### Expansion path

1.  Current NIFTY 50.
2.  NIFTY 100.
3.  NIFTY 500.
4.  Broader eligible NSE-listed equities, subject to data availability,
    liquidity filters, licensing and operational capacity.

### Universe Manager responsibilities

-   Maintain instrument identifiers, exchange symbols, ISIN where
    available, company name and sector.
-   Track membership in indices with `valid_from` and `valid_to` dates.
-   Handle symbol changes, mergers, delistings and corporate actions.
-   Apply configurable eligibility and liquidity filters.
-   Keep historical constituent snapshots for unbiased backtesting.

**Backtesting safeguard:** do not test historical periods using only
today's constituents. That creates survivorship bias.

## 4. Market-data architecture

### Provisional provider choice

-   **FYERS direct API:** first adapter candidate.
-   **Kite Connect:** optional second provider if the user has
    credentials and an appropriate subscription.
-   **NSE official data products:** evaluate when data licensing,
    authority or coverage requirements justify them.
-   Do not treat public pages or unofficial endpoints as stable,
    licensed historical-market-data APIs without verification.

The provider choice is provisional until authentication, account
permissions, history coverage, intervals, rate limits and actual
response payloads are tested.

### Market-data flow

``` text
Provider REST / WebSocket
          |
          v
Provider Adapter
          |
          v
Authentication + Rate Limit + Retry + Cache
          |
          v
Symbol / Instrument Mapping
          |
          v
OHLCV Validation
          |
          v
Canonical Candle Store
          |
          v
Session-aware timeframe builder
          |
          +--> Daily
          +--> 4-hour (aggregated respecting NSE sessions)
          +--> Hourly
          +--> Optional 15-minute
```

### Canonical candle fields

-   `symbol`, `exchange`, `instrument_id`, `timeframe`
-   `open_time`, `close_time`, timezone
-   `open`, `high`, `low`, `close`, `volume`
-   `source_provider`, `retrieved_at`
-   `is_complete`, adjustment status and quality flags
-   optional source-payload hash or raw-response reference where
    retention is permitted

### Required data-quality checks

-   OHLC invariants (`high >= open/close/low`,
    `low <= open/close/high`).
-   Duplicate and out-of-order candles.
-   Missing intervals and suspicious volume.
-   Exchange holidays, special sessions and session boundaries.
-   In-progress vs closed candles.
-   Corporate actions and adjusted/unadjusted price consistency.
-   Stale data and provider failures.

Never silently fill a missing candle with invented prices. Flag gaps and
define an explicit policy.

## 5. Swing-trading timeframe design

### Approved initial design

  -----------------------------------------------------------------------
  Timeframe               Role                    Example analysis
  ----------------------- ----------------------- -----------------------
  **Daily (1D)**          Primary trend           EMA 100/200, major
                                                  support/resistance,
                                                  volume, HH/HL/LH/LL

  **4-hour (4H)**         Intermediate trend      EMA 20/50, pullbacks,
                                                  supply/demand zones,
                                                  structure

  **Hourly (1H)**         Setup confirmation      Breakout/retest,
                                                  structure change,
                                                  volume confirmation

  **15-minute (15m)**     Optional entry          Refine entry timing
                          refinement              only if backtests show
                                                  benefit

  **1-minute (1m)**       Not part of initial     May be retained for
                          swing strategy          separate research; not
                                                  used in primary signal
                                                  logic
  -----------------------------------------------------------------------

### Processing policy

-   Use closed candles for confirmed signals.
-   Build 4H candles using session-aware boundaries; do not combine
    candles across unrelated sessions.
-   Every analysis must be evaluated against an explicit `as_of`
    timestamp in `Asia/Kolkata`.
-   The UI should display the timestamp and finality of every timeframe
    used.
-   Do not run the full multi-agent graph every five minutes for every
    stock. Refresh market data efficiently; trigger full analysis on a
    new relevant setup or material event.

## 6. News, policy and corporate-event intelligence

### Event sources

1.  RBI policy statements, circulars and official publications.
2.  Government ministry and regulator notifications relevant to each
    sector.
3.  NSE corporate announcements and financial filings.
4.  Company investor-relations news, results, presentations and reports.
5.  Authorized news feeds/RSS and web sources.
6.  Wigolo MCP as a candidate for web search, page retrieval, crawling
    and extraction, subject to capability, licence, reliability and
    access-policy verification.

### Example events

-   Repo-rate changes and monetary-policy decisions.
-   RBI inspections, restrictions or regulatory action involving a bank.
-   Quarterly results, guidance, material profit changes or
    asset-quality deterioration.
-   Merger, acquisition, fraud investigation or management changes.
-   NIFTY 50 / NIFTY BANK constituent changes.
-   Government policy changes affecting a sector.
-   Material corporate actions.

### Event processing workflow

``` text
Search / RSS / Official disclosure
                |
                v
          Fetch source page
                |
                v
   Extract headline, body, dates, entities
                |
                v
      Deduplicate / cluster related stories
                |
                v
   Verify with official source where possible
                |
                v
      Link event to company / sector / index
                |
                v
      Assess severity, direction, confidence
                |
                v
    Store source evidence and publication time
                |
                v
      Trigger analysis for affected stocks
```

### Event record

-   `event_id`, category and concise summary.
-   Company, sector and index references.
-   `published_at`, `effective_at`, `retrieved_at`.
-   Source name, source URL, official/non-official flag.
-   Evidence excerpt or permitted document reference.
-   Severity, potential direction (positive/negative/mixed/uncertain),
    confidence.
-   Reasoning, uncertainty and corroboration status.

Severity and direction are hypotheses to evaluate, not trading
instructions. For example, a repo-rate increase may affect different
banks differently depending on deposit costs, lending yields, liquidity
and market expectations.

### Wigolo and Moneycontrol constraints

-   Wigolo should be isolated behind a `WebResearchAdapter` so it can be
    replaced if it is unreliable or unsuitable.
-   Prefer official sources and authorized feeds for critical events.
-   Moneycontrol has useful financial pages, but its terms restrict
    automated scraping and machine-readable database use without prior
    express written permission. **Do not implement automated
    Moneycontrol collection unless written permission or a suitable
    licence is obtained.**
-   Publicly accessible HTML does not by itself establish permission to
    automate collection, store, process with AI or redistribute the
    content.

## 7. Fundamental-data architecture

### Source hierarchy

1.  **NSE financial results / integrated filings / XBRL:** primary
    official listed-company disclosure source.
2.  **Company investor-relations websites:** annual reports, results,
    presentations, transcripts and explanatory context.
3.  **RBI / official government sources:** macroeconomic,
    monetary-policy and sector information.
4.  **Moneycontrol:** secondary comparison/discovery only if
    automated-use permission/licensing is secured.
5.  **Licensed financial-data API/vendor:** evaluate if normalized
    coverage across the universe cannot be maintained reliably from
    official filings.

### Common company fundamentals

-   Quarterly and annual P&L / income statement.
-   Balance sheet.
-   Cash-flow statement when disclosed.
-   EPS and reported financial ratios.
-   Standalone vs consolidated values.
-   Original vs revised filings.
-   Corporate actions and material disclosures.

### Sector-specific metrics

-   **Banks:** NII, NIM, gross/net NPA, provision coverage, credit cost,
    capital adequacy, deposits, advances and CASA where disclosed.
-   **NBFCs:** AUM, asset quality, funding mix, credit cost and
    capital/liquidity measures where disclosed.
-   **IT:** revenue growth, operating margins, constant-currency growth
    and deal/order commentary where disclosed.
-   **Consumer/FMCG:** volume growth, gross/operating margins and
    input-cost commentary where disclosed.
-   **Industrials:** order inflows, order book, execution, working
    capital and capex.
-   **Energy/commodities:** volumes, realizations, spreads, leverage and
    capex as relevant.
-   Other sectors receive suitable metric schemas; missing metrics
    remain missing rather than being guessed.

### Financial fact model

-   `company_id`, canonical `metric_code`, original reported label.
-   Value, unit and currency.
-   Period start/end and period type.
-   Standalone/consolidated scope.
-   Source, source document and URL.
-   `published_at` / exchange broadcast timestamp and `retrieved_at`.
-   Original/revised status and revision linkage.
-   Extraction method, validation status and source-document
    hash/reference.

**Critical rule:** a quarter-end date is not the date a result became
public. Backtests must not use results until the actual publication
time.

## 8. Event impact and swing-analysis workflow

``` text
                  Scheduled / User-triggered run
                                |
                                v
                      Resolve stock universe
                                |
                                v
                 Load data available at `as_of`
                                |
             +------------------+------------------+
             |                  |                  |
             v                  v                  v
        Daily / 4H / 1H     News & policy       Fundamentals
        technicals          events              & filings
             |                  |                  |
             +------------------+------------------+
                                |
                                v
                   Validate evidence and timestamps
                                |
                                v
                  Company / sector exposure mapping
                                |
                                v
                   Technical setup + event context
                                |
                                v
                     Existing LangGraph workflow
                                |
                                v
                  Research debate and risk assessment
                                |
                                v
                  Structured report / UI / audit record
```

### Final output per stock

-   Trend by timeframe and conflicting signals.
-   Technical setup, invalidation level and relevant support/resistance.
-   Important recent company, sector and macro events with sources.
-   Fundamental trend and relevant sector-specific metrics.
-   Potential impact, direction, confidence and rationale.
-   Data freshness and missing-data warnings.
-   Risks, alternative interpretations and conditions that invalidate
    the thesis.
-   Optional entry/stop/target estimates for research, with
    deterministic risk calculations.
-   No automatic order placement.

The system should distinguish **event severity**, **estimated
fundamental impact**, **technical setup quality** and **observed market
reaction** as separate fields.

## 9. Existing TradingAgents workflow integration

Preserve the existing core graph and adapt data access and prompts
around it.

Proposed mapping:

  -----------------------------------------------------------------------
  Existing capability                 India adaptation
  ----------------------------------- -----------------------------------
  Market analyst                      NSE OHLCV, multi-timeframe
                                      indicators and market context

  News analyst                        Company news, RBI, government,
                                      sector and index events

  Fundamentals analyst                Normalized official filings and
                                      sector-specific metrics

  Sentiment analyst                   Optional later phase; use only
                                      permitted, attributable sources

  Bull/bear research                  Debate using cited evidence and
                                      explicit uncertainty

  Trader agent                        Synthesize swing setup; no order
                                      execution

  Risk debate                         Risk/reward, data conflicts,
                                      liquidity and event risks

  Portfolio manager                   Produce final structured research
                                      result; portfolio allocation only
                                      if separately approved

  Memory/reflection                   Store past analyses and realized
                                      outcomes without leaking future
                                      data into historical decisions
  -----------------------------------------------------------------------

Use deterministic Python functions for indicators, financial arithmetic,
timestamp rules and risk calculations. LLMs should interpret validated
evidence, not calculate numeric facts from screenshots or invent missing
values.

## 10. Storage, APIs and UI

### Initial storage recommendation

Start local-first and avoid distributed infrastructure until measured
load requires it.

-   **PostgreSQL:** recommended if persistent multi-stock data, event
    records, financial facts, users and analysis history are needed from
    the start.
-   **SQLite:** acceptable for a small single-user prototype, but plan a
    migration if concurrent tasks and history grow.
-   **File/object storage:** optional archive for source documents/raw
    responses where licensing and retention policies allow.
-   **Redis/vector database:** not required initially; add only when a
    measured use case justifies it.

### Backend

-   FastAPI for analysis requests, configuration, status, historical
    results and UI data.
-   Background worker/scheduler for market-data refresh, event
    monitoring and batch stock analysis.
-   Provider adapters with typed contracts and shared validation.
-   Read-only API keys where possible; credentials in environment/secret
    store, never committed to source control.

### UI recommendation

**Recommended first version: Streamlit** for the fastest path to a
useful Python-native workflow dashboard. It can expose: - Stock-universe
coverage and provider health. - Latest market-data timestamp and missing
candles. - Stock search and timeframe summaries. - Technical setup,
events, financial facts and clickable sources. - LangGraph workflow
steps, errors and run history. - Backtest controls and results.

If the UI later requires a polished multi-user product, consider a
separate React/Next.js frontend over the FastAPI backend. Do not build
both frontends in the initial implementation.

### LangSmith

-   Add tracing in the dedicated observability phase.
-   Trace graph runs and key agent/tool boundaries.
-   Record latency, failures, token usage/cost where available, source
    IDs and run configuration.
-   Avoid sending credentials or unnecessary sensitive data to traces.
-   Provide a configuration switch for local/offline runs and data that
    must not leave the machine.

## 11. Scheduling and scaling

Avoid an LLM call for every stock on every candle.

Suggested initial cadence: - Market candles: fetch/cache according to
provider capabilities and required timeframe. - Daily analysis: after
the daily candle is complete. - 4H / 1H setup checks: after each
configured candle closes. - News and policy: periodic monitoring plus
event-triggered analysis. - Fundamentals: on new filings, plus periodic
reconciliation. - Full multi-agent analysis: when a meaningful setup or
material event appears, or when the user requests it. - Daily summary:
aggregate top setups, event risks, data gaps and sector-level
developments.

For universe expansion, use a staged pipeline: 1. Ingest and validate
data for the whole universe. 2. Run cheap deterministic filters. 3.
Select candidates by configured liquidity/setup/event criteria. 4.
Invoke the full multi-agent workflow only for candidates. 5. Keep an
audit trail of skipped stocks and the reason they were skipped.

This keeps model cost and latency manageable without silently excluding
stocks.

## 12. Backtesting and evaluation

Backtesting must be a separate, deterministic engine rather than relying
on LLM-generated retrospective explanations.

Must include: - Point-in-time market candles and financial publication
timestamps. - Historical index constituents and delisted symbols where
data is available. - Corporate actions and a documented
adjusted/unadjusted-price policy. - Exchange sessions, holidays and
special sessions. - Brokerage, taxes/fees, slippage and realistic
fills. - Liquidity/volume constraints. - Clear entry, exit, stop and
target rules. - Train/validation/test or walk-forward evaluation; avoid
optimizing on the final test period. - Benchmarks and drawdown/return
metrics, not win rate alone. - Event studies that separate publication
time from subsequent market reaction. - A record of data source,
configuration and strategy version for each run.

## 13. Security, licensing and operating safeguards

-   Read-only market-data integrations by default.
-   No live order placement in the initial architecture.
-   No secrets in code, Markdown, logs or LangSmith traces.
-   Respect broker API terms, NSE data policy, source website terms and
    redistribution restrictions.
-   Store source URLs and publication timestamps for auditability.
-   Do not scrape Moneycontrol automatically without written
    permission/licence.
-   Mark stale or incomplete data; do not generate confident results
    when required inputs are missing.
-   Distinguish sourced facts, deterministic calculations, model
    interpretations and uncertainty in the UI.

## 14. Proposed implementation phases

  -----------------------------------------------------------------------
  Phase                   Work                    Exit criteria
  ----------------------- ----------------------- -----------------------
  1--4                    Repository review,      Completed research
                          India plan, market-data reports; no code
                          research,               changes
                          fundamental-source      
                          research                

  **5A**                  Architecture contracts  Existing graph still
                          and regression tests    runs; typed schemas
                                                  approved

  **5B**                  Stock universe and      NIFTY 50 instruments,
                          India calendar          membership snapshots
                                                  and session tests

  **5C**                  First market-data       Authentication,
                          adapter                 historical candles,
                                                  rate limits and quality
                                                  checks verified

  **5D**                  Timeframe builder and   Daily/4H/1H outputs
                          technical indicators    verified against test
                                                  fixtures

  **5E**                  News/policy event       Sources, timestamps,
                          adapter and event model deduplication and
                                                  evidence storage tested

  **5F**                  Official filings/XBRL   Parsed values reconcile
                          and issuer-document     to source documents;
                          ingestion               revisions preserved

  **5G**                  Multi-stock workflow    Candidate selection and
                          integration             stock-specific reports
                                                  work for NIFTY 50

  **6**                   UI and workflow         User can inspect stock
                          visualization           analysis, sources, runs
                                                  and failures

  **7**                   LangSmith tracing       Trace coverage, error
                                                  visibility and secret
                                                  redaction verified

  **8**                   Backtesting and         No look-ahead leakage;
                          evaluation              transaction costs and
                                                  point-in-time universe
                                                  handled

  **9**                   End-to-end tests,       Reproducible setup,
                          documentation and       data-quality tests and
                          hardening               known limitations
                                                  documented
  -----------------------------------------------------------------------

Implementation must still be delivered in small approved increments.
This document does not authorize every implementation task
automatically.

## 15. Decisions for review

Mark each as **Approve**, **Change**, or **Defer**.

  -----------------------------------------------------------------------
  Decision                Proposed default        Your choice
  ----------------------- ----------------------- -----------------------
  Initial universe        All current NIFTY 50    
                          constituents            

  Expansion path          NIFTY 100 → NIFTY 500 → 
                          eligible NSE universe   

  Core timeframes         Daily + session-aware   
                          4H + 1H                 

  Entry refinement        Optional 15-minute      

  Primary market adapter  FYERS direct API,       
                          pending                 
                          credential/endpoint     
                          validation              

  Secondary market        Kite Connect optional   
  adapter                                         

  News/web research       Wigolo behind an        
                          adapter plus            
                          official/authorized     
                          sources                 

  Moneycontrol automation Only after written      
                          permission/licence      

  Official financial data NSE filings/XBRL +      
                          company IR              

  Storage                 PostgreSQL if           
                          multi-stock persistence 
                          is needed; SQLite only  
                          for prototype           

  UI                      Streamlit first;        
                          FastAPI backend         

  Observability           LangSmith in its own    
                          phase, with             
                          redaction/offline       
                          option                  

  Execution               Read-only; no live      
                          orders                  

  Full LLM workflow       Trigger on candidate    
                          setup/material event or 
                          user request            
  -----------------------------------------------------------------------

## 16. Source references

-   TradingAgents official documentation:
    https://tauricresearch.github.io/TradingAgents/
-   NSE Financial Results:
    https://www.nseindia.com/companies-listing/corporate-filings-financial-results
-   NSE Integrated Filing:
    https://www.nseindia.com/companies-listing/corporate-integrated-filing
-   NSE XBRL information:
    https://www.nseindia.com/static/companies-listing/xbrl-information
-   NSE market-data policy:
    https://www.nseindia.com/static/market-data/nse-data-policy
-   NSE real-time data products:
    https://www.nseindia.com/static/market-data/real-time-data-subscription
-   FYERS API limits and history:
    https://support.fyers.in/portal/en/kb/articles/what-are-the-fyers-api-rate-limits-for-standard-and-prime-plans
-   Kite historical candles:
    https://kite.trade/docs/connect/v3/historical/
-   Moneycontrol Terms of Use:
    https://www.moneycontrol.com/cdata/termsofuse.php
-   RBI Database on Indian Economy: https://dbieold.rbi.org.in/DBIE/

------------------------------------------------------------------------

## Current status

**Architecture document generated for review.** This is a proposal, not
an implementation report. The next step is to review the decisions in
Section 15, then approve the first small implementation increment. No
source code has been modified by creating this document.

---

## Implementation addendum (10 October 2026)

The implementation now includes a user-triggered background NIFTY 50 scan, deterministic Daily/4H/1H technical analysis, optional 15-minute refinement, a combined LangGraph research workflow over point-in-time technical/news/fundamental evidence, Wigolo REST integration with RSS search fallback, sourced CSV/XBRL financial ingestion, a permission-gated Moneycontrol table fetcher, paper trading, optional LangSmith tracing and a live FYERS LIMIT/CNC order adapter behind multiple explicit gates.

Live trading remains off by default. The current live adapter does not automatically place linked stop-loss/target orders, so it is not a production-ready unattended execution system. For exact feature status, validation results and outstanding limitations, see `IMPLEMENTATION_STATUS.md`.

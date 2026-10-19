# TradingAgents India --- Phase 4: Fundamental Data Source Research

**Status:** Phase 4 research complete --- awaiting approval before Phase
5.\
**Research date:** 9 October 2026\
**Coverage target:** All current NIFTY 50 constituents initially, with
an architecture designed to expand to NIFTY 100, NIFTY 500, and eligible
NSE-listed equities.\
**Trading style:** Swing trading; daily + session-aware 4H + 1H core
timeframes, optional 15-minute entry refinement.\
**Implementation status:** No application code changed.

## 1. Executive decision

Use a **source-of-truth hierarchy** rather than relying on one website
for every field:

1.  **NSE corporate filings / XBRL** for listed-company financial
    results, announcements, shareholding and filing timestamps.
2.  **Company investor-relations websites** for annual reports, investor
    presentations, earnings-call transcripts and supplementary
    explanations.
3.  **RBI and official government sources** for macroeconomic,
    monetary-policy, banking and sector policy data.
4.  **Moneycontrol** as a secondary reference for financial summaries,
    ratios and discovery --- **but do not scrape it unless Moneycontrol
    grants prior express written permission**. Its published terms
    explicitly prohibit automated scraping and machine-readable database
    use without written authorization.
5.  If a normalized, machine-readable fundamental feed is required at
    scale, evaluate a licensed financial-data API/vendor rather than
    treating public website HTML as a permitted API.

This is an important implementation constraint: Moneycontrol has useful
financial pages, including income statement, balance sheet, cash flow,
ratios and quarterly/annual views, but the public website is not
permission to automate collection.

## 2. Source comparison

  --------------------------------------------------------------------------------------------------------------
  Source             Available information     Strengths            Constraints / risks     Recommended role
  ------------------ ------------------------- -------------------- ----------------------- --------------------
  **NSE financial    Quarterly/annual          Exchange-published   Some documents are PDFs **Primary source for
  results and        financial results,        disclosures;         or attachments; schemas official financial
  integrated         standalone/consolidated   timestamped; XBRL    and taxonomies vary by  filings**
  filings**          classification,           enables structured   company/sector; verify  
                     original/revised filings, extraction           permitted automated     
                     broadcast timestamp,                           access and reuse        
                     filing attachments and                                                 
                     XBRL where provided                                                    

  **NSE XBRL         Taxonomies and filing     Better normalization Must support            **Primary
  taxonomy and       formats, including        than parsing PDFs;   taxonomy/version        machine-readable
  filing             separate financial-result sector-specific      differences and         route when a filing
  information**      templates for banking,    filing formats       missing/optional facts  is available**
                     NBFCs, insurance and                                                   
                     other companies                                                        

  **Company investor Annual reports, financial Official issuer      Site structures differ; **Primary supplement
  relations**        results, investor         context; useful for  documents may be PDFs;  and cross-check**
                     presentations,            explanations and     release time and        
                     earnings-call             supplemental data    document version need   
                     transcripts, press                             capture                 
                     releases, board notices                                                
                     and other disclosures                                                  

  **Moneycontrol**   Financial overview,       Convenient           Terms prohibit          **Secondary
                     income statement, balance normalized           bots/scrapers and       reference only
                     sheet, cash flow, ratios, presentation and     machine-readable        unless written
                     quarterly and annual      discovery            database use without    permission/licence
                     financial views; company                       prior express written   is obtained**
                     news                                           permission;             
                                                                    accuracy/completeness   
                                                                    not guaranteed; no      
                                                                    public authorized API   
                                                                    was verified in this    
                                                                    research                

  **RBI Database on  Policy rates, inflation,  Official macro and   Series may have release **Primary source for
  Indian Economy     banking statistics,       banking-sector       calendars, revisions    RBI/macro context**
  (DBIE)**           credit, deposits,         context              and differing           
                     macroeconomic time series                      periodicity; not a      
                     and RBI publications                           substitute for          
                                                                    individual company      
                                                                    filings                 

  **Government       Policy notifications,     Authoritative source Relevant sources differ **Primary source for
  ministry /         rules, circulars, budget  for policy text and  by sector; documents    government/sector
  regulator          measures, sector-specific effective dates      require classification  policy events**
  sources**          changes                                        and impact mapping      

  **Licensed         Potentially normalized    May reduce           Cost, field coverage,   **Alternative if
  financial-data     statements, ratios and    extraction and       history, redistribution official filing
  provider (to be    corporate actions through maintenance burden   rights, timestamps and  parsing alone is
  evaluated if       an API                                         NSE-universe coverage   insufficient**
  needed)**                                                         need procurement review 
  --------------------------------------------------------------------------------------------------------------

### Official references

-   NSE Financial Results:
    https://www.nseindia.com/companies-listing/corporate-filings-financial-results
-   NSE Integrated Filing --- Financials:
    https://www.nseindia.com/companies-listing/corporate-integrated-filing
-   NSE XBRL filing information and taxonomies:
    https://www.nseindia.com/static/companies-listing/xbrl-information
-   SEBI corporate filings guide:
    https://www.sebi.gov.in/curation/corporate_filings.html
-   Moneycontrol Terms of Use:
    https://www.moneycontrol.com/cdata/termsofuse.php
-   RBI Database on Indian Economy: https://dbieold.rbi.org.in/DBIE/
-   Example issuer investor-relations pages: Canara Bank investor
    calendar https://www.canarabank.bank.in/Investor-Calendar and
    disclosures
    https://www.canarabank.bank.in/pages/disclosure-of-events-or-information

## 3. What to collect for every NIFTY 50 company

### A. Common financial data

-   Quarterly and annual income statement / profit and loss.
-   Balance sheet.
-   Cash-flow statement, where disclosed.
-   Standalone and consolidated variants.
-   Reported EPS and other disclosed ratios.
-   Shareholding pattern, corporate actions and material filings where
    relevant.
-   Annual reports and management commentary.
-   Filing publication timestamp, original/revised status and source
    document.

### B. Sector-aware fields

Do not apply one generic ratio set to every stock.

  -----------------------------------------------------------------------
  Sector type                         Additional fields / considerations
  ----------------------------------- -----------------------------------
  Banks                               Net interest income (NII), net
                                      interest margin (NIM), gross/net
                                      NPA, provision coverage, credit
                                      cost, capital adequacy, deposits,
                                      advances, CASA where reported

  NBFCs / financial services          AUM, borrowing/funding mix, asset
                                      quality, credit cost, capital
                                      adequacy and liquidity where
                                      reported

  IT services                         Revenue growth, operating margin,
                                      constant-currency growth where
                                      reported, deal wins, order book,
                                      attrition and guidance

  Consumer / FMCG                     Volume growth where disclosed,
                                      gross margin, operating margin,
                                      input-cost commentary,
                                      distribution/segment growth

  Industrials / capital goods         Order inflows, order book,
                                      execution, margins, working capital
                                      and capex

  Energy / commodities                Realizations, production/volume,
                                      refining/marketing spreads or other
                                      relevant operating metrics,
                                      leverage and capex

  Telecom                             Subscriber metrics, ARPU, churn,
                                      capex and leverage where reported

  Auto                                Volumes, mix, realizations,
                                      margins, order book or bookings
                                      where disclosed

  Insurance                           Premiums, claims, solvency and
                                      sector-specific measures where
                                      reported
  -----------------------------------------------------------------------

The system should use company/sector-aware schemas and mark unavailable
fields as unavailable; it must not infer or invent absent values.

## 4. Moneycontrol: feasibility and policy conclusion

Moneycontrol's financial pages expose useful structured-looking views
for financial overview, income statement, balance sheet, cash flow,
ratios and quarterly/annual results. However, its Terms of Use state
that, without prior express written permission, users may not: - use
automated bots, scrapers, spiders or crawlers to access, monitor,
download, copy or extract content; or - use its information in
searchable/machine-readable form as a database, including for AI
tooling.

Therefore:

-   **Do not implement a Moneycontrol scraper by default.**
-   If Moneycontrol is a required source, first obtain written
    permission or a suitable licensed data arrangement, including rights
    for storage, AI processing, historical use and display.
-   Until permission is verified, Moneycontrol can be used by a human
    analyst as a reference or as a comparison source under the
    applicable terms, not as an automated ingestion dependency.
-   Do not assume that a page being public, HTML being readable, or an
    unofficial endpoint working implies permission to automate or reuse
    it.

## 5. Canonical data model

Use a normalized, source-attributed model. Suggested core entities:

### `CompanyInstrument`

-   `company_id`
-   `symbol`
-   `exchange`
-   `isin` (when available)
-   `company_name`
-   `sector`, `industry`
-   `index_memberships` with `valid_from` and `valid_to`

### `FinancialFact`

-   `company_id`
-   `metric_code` (canonical normalized name)
-   `reported_metric_name`
-   `value`
-   `unit`, `currency`
-   `period_type` (quarter / half-year / nine-month / annual)
-   `period_start`, `period_end`
-   `scope` (standalone / consolidated)
-   `accounting_standard` / taxonomy where available
-   `source_name`, `source_document_id`, `source_url`
-   `published_at` / exchange `broadcast_at`
-   `retrieved_at`
-   `filing_status` (original / revised)
-   `revision_of`, `revision_published_at`
-   `extraction_method`, `validation_status`
-   `raw_document_hash` or archived document reference, where storage is
    permitted

### `FinancialStatement`

Store a parsed statement's line items with their original labels and
normalized metric codes. Preserve the original filing/document
reference; do not discard the reported wording or units.

### `CorporateEvent`

-   `company_id` and/or sector/index references
-   `event_type`, `summary`, `source`
-   `published_at`, `effective_at`, `retrieved_at`
-   `potential_severity`, `direction`, `confidence`
-   evidence references and uncertainty flags

### `MacroSeries`

-   `series_code`, `value`, `unit`, `observation_period`
-   `published_at`, `retrieved_at`, `source`, `revision_status`

## 6. Point-in-time and revision safeguards

This is mandatory for credible backtesting.

1.  **Period end is not publication time.** A result for a quarter
    ending 31 March cannot be used in a simulated decision until its
    actual public release time.
2.  **Preserve original and revised filings.** Do not overwrite old
    facts when a company files a revision.
3.  **Record source timestamps.** At minimum, retain
    publication/broadcast time and retrieval time.
4.  **Track consolidated versus standalone.** Never compare one basis to
    another without making the distinction explicit.
5.  **Track units and signs.** Normalize crore/lakh/rupee, percentages,
    per-share values and negative values explicitly.
6.  **Validate statement arithmetic where appropriate.** Flag
    mismatches; do not silently force values to reconcile.
7.  **Treat missing values as missing.** Do not fill absent disclosures
    with LLM guesses.
8.  **Respect release calendars and late filings.** A scheduled release
    date is not proof that the data was actually published at that time.
9.  **Use point-in-time index membership.** Historical backtests must
    include the stocks that were members at the historical decision
    time, including later removals.

## 7. Fundamental-event and impact workflow

``` text
NSE Filing / XBRL        Company IR        RBI / Government
       |                    |                    |
       +--------------------+--------------------+
                            |
                   Source + Timestamp Capture
                            |
                 Document / XBRL Extraction
                            |
                Validation + Normalization
                            |
                Original / Revised Versioning
                            |
                  Point-in-Time Data Store
                            |
        +-------------------+-------------------+
        |                   |                   |
  Financial Metrics    Corporate Events     Macro / Policy
        |                   |                   |
        +-------------------+-------------------+
                            |
                  Company / Sector Mapping
                            |
                Fundamental & Event Analysis
                            |
                 TradingAgents Research Flow
                            |
           Evidence, Conflicts, Risks, Source Links
```

Moneycontrol may be added as a source node only after written
authorization/licensing is confirmed.

## 8. Recommended ingestion and refresh model

-   **Financial statements:** event-driven on new filings, with periodic
    reconciliation to detect missed disclosures.
-   **Company IR documents:** check on a schedule and after relevant
    company announcements.
-   **RBI / policy sources:** periodic monitoring plus event-triggered
    processing for material notices.
-   **Normalized ratios:** calculate deterministic ratios from validated
    source facts where possible; store vendor-reported ratios separately
    and compare them rather than treating them as interchangeable.
-   **NIFTY universe:** start with current NIFTY 50 membership, but
    retain dated constituent snapshots and support later expansion to
    NIFTY 100, NIFTY 500 and the broader eligible NSE universe.
-   **LLM use:** summarize, classify and explain validated evidence; do
    not use an LLM as the authority for numeric extraction or
    arithmetic.

## 9. Decisions requested before Phase 5

Please approve or revise the following:

1.  NSE filings/XBRL as the primary source for official financial
    results.
2.  Company investor-relations pages as the official supplementary
    source.
3.  RBI and government websites as primary sources for macro and policy
    events.
4.  Moneycontrol as a secondary source **only if automated-use
    permission/licensing is secured**; no unauthorized scraping.
5.  Build a point-in-time normalized financial-fact store with revision
    tracking.
6.  Use sector-aware fundamentals, not one generic financial schema.
7.  Start with all NIFTY 50 constituents, while designing for future
    NSE-wide expansion.

**Phase 4 status: complete. Awaiting approval before Phase 5
implementation.**

Per the requested workflow, a separate Markdown architecture/flow
decision document can be generated in the next turn after this research
report has been reviewed.

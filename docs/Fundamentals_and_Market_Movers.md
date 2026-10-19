# Fundamentals and Market Movers workspaces

## Fundamental Analysis

- Uses `tradingagents/india/data/nifty_500_constituents.csv` as the current 500-stock universe snapshot.
- Displays a transparent, heuristic score based on stored statement flags and fresh valuation snapshots. The score is a research triage tool, not an intrinsic-value model or buy/sell signal.
- Shows the positive/negative evidence behind each status. Missing evidence is not interpreted as positive evidence.
- Clicking a stock selects it globally and loads its existing valuation, financial flags, charts, statement tables, and sourced raw facts.
- `Refresh fundamentals` on a stock fetches provider data. `Refresh all (Nifty 500)` uses the existing background bulk-refresh job and may take a long time.
- Yahoo Finance values are provider snapshots, not official filings. Prefer NSE/company filings for verification.

API: `GET /api/fundamental-screen?universe=NIFTY%20500`; add `refresh=true` to bypass the 10-minute cache.

## Market Movers

- Uses daily Yahoo Finance bars for the Nifty 500 constituent list.
- Shows top percentage gainers, top percentage losers, highest share volume, breadth counts, and a searchable/sortable full table.
- Estimated traded value is `close × volume`; it is not exchange-reported turnover. The feed is not a real-time quote stream, and provider coverage may vary.
- Results are cached for five minutes unless `refresh=true` is requested.

API: `GET /api/market/movers?universe=NIFTY%20500`; add `refresh=true` to fetch again.

## Responsive UI

The new workspaces use responsive card grids, a compact mobile navigation drawer, full-width mobile filters, and horizontally scrollable data tables. The historical candle and CSV-import controls remain separate from the Fundamental Analysis workspace.

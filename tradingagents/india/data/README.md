# India universe and calendar seed data

These CSVs are versioned seed inputs, not live endpoints. They do not make network requests.

## NIFTY 50 constituents

`nifty50_constituents.csv` contains a 50-instrument snapshot effective **30 September 2026**. It is intended to bootstrap the initial universe only. The symbols reflect the NIFTY 50 constituent listing and the September 2026 reconstitution (BSE included, WIPRO excluded).

Official references:

- NSE NIFTY 50 index page and constituent download: https://www.nseindia.com/static/products-services/indices-nifty50-index
- NSE live equity market (NIFTY 50 view): https://www.nseindia.com/market-data/live-equity-market?cat=EW50

The NSE index page's displayed content update date may lag a later reconstitution notice. For operational refreshes, confirm the latest effective-date circular before updating this file.

### Refresh procedure

1. Download the latest official constituent list or verify the latest index reconstitution notice.
2. Add a new set of rows with the official effective date as `snapshot_date`; do not overwrite old snapshots.
3. Keep `source_name` and `source_url` populated on every row.
4. Ensure each index/date snapshot contains unique symbols and the expected constituent count.
5. Never assume the current constituent list represents the historical universe before its snapshot date. If there is no snapshot on or before a backtest date, `StockUniverseRegistry` raises `UniverseHistoryUnavailable`.

For NIFTY 100 / NIFTY 500, append separate snapshots using the same schema and official source metadata.

## NSE equity holidays

`nse_equity_holidays_2026.csv` encodes the 2026 equity-market holiday list shown on the official NSE exchange communication page. The 8 November 2026 Muhurat Trading date is represented as a special session with unknown hours because the NSE page states that timings will be notified separately.

Official references:

- NSE market timings and holidays: https://www.nseindia.com/resources/exchange-communication-holidays
- NSE market timing table: https://www.nseindia.com/static/market-data/market-timings

The calendar is intentionally configured for **2026 only**. It raises `CalendarCoverageError` for other years rather than assuming a weekday is a trading day. Add and validate a new official holiday file before using a new year or an older historical period.

## Data-use caution

The CSVs are bootstrap configuration, not a substitute for a source refresh process. The application should retain snapshot dates, source provenance and historical versions for point-in-time backtesting. Exchange notices, special sessions, and holidays can change; verify them against official NSE notices before production use.


## FYERS market-data adapter (Phase 5C)

The read-only adapter is implemented in `tradingagents.india.providers.fyers`. It calls the documented FYERS v3 history endpoint and normalizes candles into `OHLCVCandle`. It does not place orders.

Configure `FYERS_APP_ID` and `FYERS_ACCESS_TOKEN` in your local environment or a local `.env` file. Do not paste credentials into chat or commit them. The access token is sensitive and may need renewal each trading day. The adapter does not perform interactive OAuth login or refresh-token handling; account authentication must be completed through the official FYERS flow first.

Example (after configuring the environment):

```python
from datetime import date, timedelta
from tradingagents.india import FyersMarketDataClient, SwingTimeframe

client = FyersMarketDataClient.from_env()
candles = client.get_history(
    "SBIN",
    date.today() - timedelta(days=180),
    date.today(),
    SwingTimeframe.DAILY,
)
print(f"Received {len(candles)} daily candles")
```

The adapter uses conservative in-process request throttling based on FYERS Standard Data API limits by default (5 requests/second, 50/minute, 5,000/day); override `FyersRateLimits` only to match the account's actual plan. The daily budget is tracked in memory for this process, so do not run multiple independent adapter processes against one token and assume the quota is coordinated globally.

Resolution mapping: daily → `D`; hourly → `60`; 15-minute → `15`; 4-hour → aggregate hourly candles into session-anchored bars starting 09:15 IST. The final 4-hour bar is a partial-session bar (normally 13:15–15:30). Candle timestamps are normalized to `Asia/Kolkata`; incomplete current candles are flagged. Historical requests are chunked to provider request windows (366 calendar days for daily, 100 calendar days for intraday) and duplicate timestamps are de-duplicated.

**Verification status:** unit tests use mocked HTTP responses. No live FYERS call has been made because no credentials were configured in this workspace. Authentication, permission scope, account plan, and actual live payloads must be verified with the user's own FYERS account before treating the adapter as operational.

### Smoke-test your own credentials

After filling `FYERS_APP_ID` and `FYERS_ACCESS_TOKEN` in a local `.env` file, run:

```bash
python scripts/check_fyers_history.py --symbol SBIN
```

Optionally pass `--from-date YYYY-MM-DD --to-date YYYY-MM-DD`. The script prints only candle metadata and never prints credentials. A no-candle response can occur for a weekend/holiday or a date outside available history; use a recent trading date before concluding that authentication failed.

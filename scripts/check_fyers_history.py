#!/usr/bin/env python3
"""Smoke-test FYERS historical-data credentials without exposing the token."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow running directly from a source checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from tradingagents.india.providers.fyers import FyersError, FyersMarketDataClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SBIN", help="Canonical NSE equity symbol (default: SBIN)")
    parser.add_argument("--from-date", type=date.fromisoformat, help="Inclusive YYYY-MM-DD start date")
    parser.add_argument("--to-date", type=date.fromisoformat, help="Inclusive YYYY-MM-DD end date (default: today)")
    args = parser.parse_args()

    load_dotenv()
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    end_date = args.to_date or today
    start_date = args.from_date or (end_date - timedelta(days=7))
    try:
        client = FyersMarketDataClient.from_env()
        candles = client.get_history(args.symbol, start_date, end_date, "1d")
    except FyersError as exc:
        print(f"FYERS smoke test failed: {exc}", file=sys.stderr)
        return 2
    except (ValueError, TypeError) as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return 2

    print("FYERS history endpoint responded successfully.")
    print(f"Symbol: {args.symbol.upper()} | Range: {start_date} through {end_date}")
    print(f"Daily candles received: {len(candles)}")
    if candles:
        latest = candles[-1]
        print(
            "Latest candle: "
            f"{latest.open_time.isoformat()} | O={latest.open:g} H={latest.high:g} "
            f"L={latest.low:g} C={latest.close:g} V={latest.volume:g} "
            f"complete={latest.is_complete}"
        )
    else:
        print("No candles in this range. Credentials may still be valid; choose a recent trading date to confirm data availability.")
    print("No orders were submitted. Access token was not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

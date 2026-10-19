"""Offline tests for the read-only FYERS history adapter."""

import os
import unittest
from unittest.mock import patch
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tradingagents.india.contracts import SwingTimeframe
from tradingagents.india.providers.fyers import (
    FyersAuthenticationError,
    FyersConfigurationError,
    FyersDataError,
    FyersMarketDataClient,
    FyersRateLimitError,
    FyersRateLimits,
    aggregate_hourly_to_four_hour,
)

IST = ZoneInfo("Asia/Kolkata")


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def epoch_ist(year, month, day, hour, minute):
    return int(datetime(year, month, day, hour, minute, tzinfo=IST).timestamp())


def fyers_row(year, month, day, hour, minute, o=100, h=105, l=99, c=104, v=1000):
    return [epoch_ist(year, month, day, hour, minute), o, h, l, c, v]


class FyersConfigurationTests(unittest.TestCase):
    def test_requires_app_id_and_access_token(self):
        with self.assertRaises(FyersConfigurationError):
            FyersMarketDataClient("", "token")
        with self.assertRaises(FyersConfigurationError):
            FyersMarketDataClient("app-100", "")

    def test_symbol_mapping(self):
        self.assertEqual(FyersMarketDataClient.vendor_symbol("sbin"), "NSE:SBIN-EQ")
        self.assertEqual(FyersMarketDataClient.vendor_symbol("NSE:SBIN-EQ"), "NSE:SBIN-EQ")
        with self.assertRaises(ValueError):
            FyersMarketDataClient.vendor_symbol("SBIN;DROP TABLE")

    def test_from_env_reads_credentials_url_and_plan_limits(self):
        env = {
            "FYERS_APP_ID": "APP-100",
            "FYERS_ACCESS_TOKEN": "secret-token",
            "FYERS_DATA_BASE_URL": "https://example.invalid/history",
            "FYERS_REQUESTS_PER_SECOND": "10",
            "FYERS_REQUESTS_PER_MINUTE": "500",
            "FYERS_REQUESTS_PER_DAY": "500000",
        }
        with patch.dict(os.environ, env, clear=True):
            client = FyersMarketDataClient.from_env(session=FakeSession([]), sleep=lambda _: None)
        self.assertEqual(client.base_url, "https://example.invalid/history")
        self.assertEqual(client._limiter.limits.requests_per_second, 10)
        self.assertEqual(client._limiter.limits.requests_per_minute, 500)
        self.assertEqual(client._limiter.limits.requests_per_day, 500000)


class FyersHistoryTests(unittest.TestCase):
    def client(self, session, **kwargs):
        fixed_now = datetime(2026, 10, 9, 16, 0, tzinfo=IST)
        return FyersMarketDataClient(
            "APP-100", "test-token", session=session, now=lambda: fixed_now,
            sleep=lambda _: None, **kwargs
        )

    def test_fetches_and_normalizes_daily_candle(self):
        session = FakeSession([FakeResponse({"s": "ok", "candles": [fyers_row(2026, 10, 9, 9, 15)]})])
        candles = self.client(session).get_history("SBIN", date(2026, 10, 9), date(2026, 10, 9), "1d")
        self.assertEqual(len(candles), 1)
        candle = candles[0]
        self.assertEqual(candle.symbol, "SBIN")
        self.assertEqual(candle.source_provider, "fyers")
        self.assertEqual(candle.open_time, datetime(2026, 10, 9, 9, 15, tzinfo=IST))
        self.assertEqual(candle.close_time, datetime(2026, 10, 9, 15, 30, tzinfo=IST))
        self.assertTrue(candle.is_complete)
        _, kwargs = session.calls[0]
        self.assertEqual(kwargs["headers"]["Authorization"], "APP-100:test-token")
        self.assertEqual(kwargs["params"]["resolution"], "D")

    def test_fetches_hourly_and_marks_unclosed_candle(self):
        session = FakeSession([FakeResponse({"s": "ok", "candles": [fyers_row(2026, 10, 9, 15, 15)]})])
        client = FyersMarketDataClient("APP-100", "token", session=session, now=lambda: datetime(2026, 10, 9, 15, 20, tzinfo=IST), sleep=lambda _: None)
        candles = client.get_history("SBIN", date(2026, 10, 9), date(2026, 10, 9), SwingTimeframe.HOURLY)
        self.assertEqual(candles[0].close_time, datetime(2026, 10, 9, 15, 30, tzinfo=IST))
        self.assertFalse(candles[0].is_complete)
        self.assertIn("in_progress_or_unclosed_candle", candles[0].quality_flags)
        self.assertEqual(session.calls[0][1]["params"]["resolution"], "60")

    def test_discards_premarket_intraday_rows_without_failing_analysis(self):
        rows = [
            fyers_row(2026, 9, 9, 8, 15),  # pre-market row returned by provider
            fyers_row(2026, 9, 9, 9, 15),
            fyers_row(2026, 9, 9, 10, 15),
            fyers_row(2026, 9, 9, 15, 15),
        ]
        session = FakeSession([FakeResponse({"s": "ok", "candles": rows})])
        candles = self.client(session).get_history(
            "ICICIBANK", date(2026, 9, 9), date(2026, 9, 9), SwingTimeframe.HOURLY
        )
        self.assertEqual(len(candles), 3)
        self.assertTrue(all(candle.open_time.time() >= datetime(2026, 9, 9, 9, 15, tzinfo=IST).time() for candle in candles))
        self.assertTrue(all(candle.open_time.time() < datetime(2026, 9, 9, 15, 30, tzinfo=IST).time() for candle in candles))

    def test_discards_premarket_rows_for_four_hour_aggregation(self):
        rows = [
            fyers_row(2026, 9, 9, 8, 15),
            fyers_row(2026, 9, 9, 9, 15),
            fyers_row(2026, 9, 9, 10, 15),
        ]
        session = FakeSession([FakeResponse({"s": "ok", "candles": rows})])
        candles = self.client(session).get_history(
            "ICICIBANK", date(2026, 9, 9), date(2026, 9, 9), SwingTimeframe.FOUR_HOUR
        )
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].open_time, datetime(2026, 9, 9, 9, 15, tzinfo=IST))

    def test_intraday_request_chunks_at_100_days(self):
        session = FakeSession([
            FakeResponse({"s": "ok", "candles": []}),
            FakeResponse({"s": "ok", "candles": []}),
        ])
        start, end = date(2026, 1, 1), date(2026, 4, 11)  # 101 inclusive dates
        self.client(session).get_history("SBIN", start, end, "15m")
        self.assertEqual(len(session.calls), 2)
        first = session.calls[0][1]["params"]
        second = session.calls[1][1]["params"]
        self.assertEqual(first["range_from"], "2026-01-01")
        self.assertEqual(first["range_to"], "2026-04-10")
        self.assertEqual(second["range_from"], "2026-04-11")

    def test_daily_chunks_at_366_inclusive_dates(self):
        session = FakeSession([FakeResponse({"s": "ok", "candles": []}), FakeResponse({"s": "ok", "candles": []})])
        self.client(session).get_history("SBIN", date(2025, 1, 1), date(2026, 1, 2), "1d")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][1]["params"]["range_to"], "2026-01-01")
        self.assertEqual(session.calls[1][1]["params"]["range_from"], "2026-01-02")

    def test_authentication_error_is_not_retried(self):
        session = FakeSession([FakeResponse({"s": "error", "code": -8, "message": "Invalid token"})])
        with self.assertRaises(FyersAuthenticationError):
            self.client(session).get_history("SBIN", date(2026, 10, 9), date(2026, 10, 9), "1d")
        self.assertEqual(len(session.calls), 1)

    def test_malformed_response_fails_closed(self):
        session = FakeSession([FakeResponse({"s": "ok", "candles": [[123, 1, 2]]})])
        with self.assertRaises(FyersDataError):
            self.client(session).get_history("SBIN", date(2026, 10, 9), date(2026, 10, 9), "1d")

    def test_retries_server_error_then_succeeds(self):
        session = FakeSession([
            FakeResponse({"s": "error", "message": "temporarily unavailable"}, status_code=503),
            FakeResponse({"s": "ok", "candles": []}),
        ])
        candles = self.client(session).get_history("SBIN", date(2026, 10, 9), date(2026, 10, 9), "1d")
        self.assertEqual(candles, ())
        self.assertEqual(len(session.calls), 2)

    def test_four_hour_fetch_uses_hourly_resolution_and_aggregates(self):
        rows = [
            fyers_row(2026, 10, 9, 9, 15, 100, 105, 99, 103, 100),
            fyers_row(2026, 10, 9, 10, 15, 103, 108, 102, 107, 200),
            fyers_row(2026, 10, 9, 11, 15, 107, 109, 104, 105, 300),
            fyers_row(2026, 10, 9, 12, 15, 105, 110, 103, 109, 400),
            fyers_row(2026, 10, 9, 13, 15, 109, 112, 108, 111, 500),
            fyers_row(2026, 10, 9, 14, 15, 111, 113, 107, 108, 600),
            fyers_row(2026, 10, 9, 15, 15, 108, 109, 106, 107, 700),
        ]
        session = FakeSession([FakeResponse({"s": "ok", "candles": rows})])
        candles = self.client(session).get_history("SBIN", date(2026, 10, 9), date(2026, 10, 9), "4h")
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0].open_time, datetime(2026, 10, 9, 9, 15, tzinfo=IST))
        self.assertEqual(candles[0].close_time, datetime(2026, 10, 9, 13, 15, tzinfo=IST))
        self.assertEqual(candles[0].open, 100)
        self.assertEqual(candles[0].close, 109)
        self.assertEqual(candles[0].volume, 1000)
        self.assertEqual(candles[1].open_time, datetime(2026, 10, 9, 13, 15, tzinfo=IST))
        self.assertEqual(candles[1].close_time, datetime(2026, 10, 9, 15, 30, tzinfo=IST))
        self.assertIn("partial_session_bar", candles[1].quality_flags)
        self.assertEqual(session.calls[0][1]["params"]["resolution"], "60")

    def test_aggregate_rejects_non_hourly_input(self):
        daily = self.client(FakeSession([FakeResponse({"s": "ok", "candles": [fyers_row(2026, 10, 9, 9, 15)]})])).get_history(
            "SBIN", date(2026, 10, 9), date(2026, 10, 9), "1d"
        )[0]
        with self.assertRaisesRegex(ValueError, "hourly candles"):
            aggregate_hourly_to_four_hour([daily])


class FyersRateLimitTests(unittest.TestCase):
    def test_daily_budget_raises_when_exhausted(self):
        current = {"mono": 0.0, "now": datetime(2026, 10, 9, 10, 0, tzinfo=IST)}
        def sleep(seconds):
            current["mono"] += seconds
        from tradingagents.india.providers.fyers import _SlidingWindowLimiter
        limiter = _SlidingWindowLimiter(
            FyersRateLimits(requests_per_second=1, requests_per_minute=1, requests_per_day=1),
            monotonic=lambda: current["mono"], sleep=sleep, wall_clock=lambda: current["now"],
        )
        limiter.acquire()
        with self.assertRaises(FyersRateLimitError):
            limiter.acquire()


if __name__ == "__main__":
    unittest.main()

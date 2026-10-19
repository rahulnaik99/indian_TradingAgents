"""Tests for the configured NSE cash-equity session calendar."""

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from tradingagents.india.exchange_calendar import (
    CalendarCoverageError,
    NSEEquityCalendar,
    SessionScheduleUnavailable,
)

IST = ZoneInfo("Asia/Kolkata")


class NSEEquityCalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calendar = NSEEquityCalendar.builtin()

    def test_regular_session_hours(self):
        day = date(2026, 10, 9)
        session = self.calendar.session_for(day)
        self.assertIsNotNone(session)
        self.assertEqual(session.open_time.isoformat(), "09:15:00")
        self.assertEqual(session.close_time.isoformat(), "15:30:00")
        self.assertTrue(self.calendar.is_market_open(datetime(2026, 10, 9, 10, 0, tzinfo=IST)))
        self.assertFalse(self.calendar.is_market_open(datetime(2026, 10, 9, 15, 30, tzinfo=IST)))

    def test_weekends_are_not_regular_trading_days(self):
        self.assertFalse(self.calendar.is_trading_day(date(2026, 10, 10)))
        self.assertFalse(self.calendar.is_regular_session_day(date(2026, 10, 11)))

    def test_official_holiday_is_closed(self):
        day = date(2026, 10, 20)
        self.assertFalse(self.calendar.is_trading_day(day))
        self.assertIsNone(self.calendar.session_for(day))

    def test_muhurat_session_is_trading_day_but_hours_are_not_guessed(self):
        day = date(2026, 11, 8)
        self.assertTrue(self.calendar.is_trading_day(day))
        session = self.calendar.session_for(day)
        self.assertEqual(session.session_type, "special")
        self.assertFalse(session.schedule_confirmed)
        with self.assertRaises(SessionScheduleUnavailable):
            self.calendar.is_market_open(datetime(2026, 11, 8, 19, 0, tzinfo=IST))

    def test_naive_market_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.calendar.is_market_open(datetime(2026, 10, 9, 10, 0))

    def test_unsupported_year_fails_closed(self):
        with self.assertRaises(CalendarCoverageError):
            self.calendar.is_trading_day(date(2027, 1, 4))

    def test_next_trading_day_skips_weekends_and_holidays(self):
        self.assertEqual(
            self.calendar.next_trading_day(date(2026, 10, 17)),
            date(2026, 10, 19),
        )
        self.assertEqual(
            self.calendar.next_trading_day(date(2026, 10, 19)),
            date(2026, 10, 21),
        )


if __name__ == "__main__":
    unittest.main()

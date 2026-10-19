"""Unit tests for provider-neutral India-market contracts (stdlib-only)."""

import unittest
from datetime import date, datetime

from tradingagents.india.contracts import (
    AnalysisUniverse,
    CorporateEvent,
    EventDirection,
    EventSeverity,
    FinancialFact,
    FinancialScope,
    IndexMembership,
    Instrument,
    OHLCVCandle,
    SwingTimeframe,
    SwingTradingConfig,
    available_at,
    facts_available_as_of,
)

IST = "Asia/Kolkata"

def dt(hour: int, minute: int = 0) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, 9, hour, minute, tzinfo=ZoneInfo(IST))


class InstrumentContractsTests(unittest.TestCase):
    def test_membership_is_point_in_time(self):
        membership = IndexMembership("NIFTY 50", date(2025, 1, 1), date(2025, 12, 31))
        self.assertTrue(membership.contains(date(2025, 6, 1)))
        self.assertFalse(membership.contains(date(2026, 1, 1)))

    def test_membership_rejects_inverted_dates(self):
        with self.assertRaises(ValueError):
            IndexMembership("NIFTY 50", date(2026, 1, 2), date(2026, 1, 1))

    def test_instrument_keeps_vendor_neutral_identity(self):
        instrument = Instrument(
            symbol="CANARABANK",
            company_name="Canara Bank",
            sector="Financial Services",
            index_memberships=(IndexMembership("NIFTY 50", date(2024, 1, 1)),),
        )
        self.assertEqual(instrument.exchange, "NSE")
        self.assertEqual(instrument.index_memberships[0].index_name, "NIFTY 50")


class CandleContractTests(unittest.TestCase):
    def valid_candle(self, **overrides):
        values = {
            "symbol": "CANARABANK",
            "timeframe": SwingTimeframe.DAILY,
            "open_time": dt(9, 15),
            "close_time": dt(15, 30),
            "open": 100.0,
            "high": 110.0,
            "low": 98.0,
            "close": 108.0,
            "volume": 100000.0,
            "source_provider": "fixture",
            "retrieved_at": dt(15, 31),
            "is_complete": True,
        }
        values.update(overrides)
        return OHLCVCandle(**values)

    def test_valid_candle(self):
        candle = self.valid_candle()
        self.assertTrue(candle.is_complete)
        self.assertEqual(candle.timeframe, SwingTimeframe.DAILY)

    def test_rejects_naive_timestamps(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.valid_candle(open_time=datetime(2026, 10, 9, 9, 15))

    def test_rejects_invalid_ohlc(self):
        with self.assertRaisesRegex(ValueError, "high must"):
            self.valid_candle(high=105.0)

    def test_rejects_negative_volume(self):
        with self.assertRaisesRegex(ValueError, "volume cannot be negative"):
            self.valid_candle(volume=-1.0)

    def test_rejects_non_finite_values(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            self.valid_candle(close=float("nan"))


class FinancialFactContractTests(unittest.TestCase):
    def fact(self, **overrides):
        values = {
            "symbol": "CANARABANK",
            "metric_code": "net_profit",
            "value": 5000.0,
            "unit": "INR_CRORE",
            "currency": "INR",
            "period_start": date(2026, 1, 1),
            "period_end": date(2026, 3, 31),
            "period_type": "quarter",
            "scope": FinancialScope.CONSOLIDATED,
            "source_name": "NSE",
            "source_url": "https://example.invalid/filing",
            "published_at": dt(18, 0),
            "retrieved_at": dt(18, 5),
        }
        values.update(overrides)
        return FinancialFact(**values)

    def test_publication_timestamp_controls_availability(self):
        fact = self.fact()
        self.assertFalse(available_at(fact, dt(17, 59)))
        self.assertTrue(available_at(fact, dt(18, 0)))
        self.assertEqual(facts_available_as_of([fact], dt(17, 59)), ())
        self.assertEqual(facts_available_as_of([fact], dt(18, 1)), (fact,))

    def test_revised_fact_requires_revision_link(self):
        with self.assertRaisesRegex(ValueError, "revision_of"):
            self.fact(filing_status="revised")

    def test_rejects_period_end_before_start(self):
        with self.assertRaisesRegex(ValueError, "period_end"):
            self.fact(period_start=date(2026, 4, 1), period_end=date(2026, 3, 31))

    def test_rejects_naive_publication_timestamp(self):
        with self.assertRaisesRegex(ValueError, "published_at"):
            self.fact(published_at=datetime(2026, 10, 9, 18, 0))


class CorporateEventContractTests(unittest.TestCase):
    def test_event_captures_impact_and_evidence_fields(self):
        event = CorporateEvent(
            event_id="event-1",
            event_type="rbi_policy",
            summary="Policy announcement",
            source_name="RBI",
            source_url="https://example.invalid/policy",
            published_at=dt(10),
            retrieved_at=dt(10, 1),
            severity=EventSeverity.HIGH,
            direction=EventDirection.UNCERTAIN,
            confidence=0.7,
            affected_symbols=("CANARABANK",),
            is_official_source=True,
        )
        self.assertTrue(event.is_official_source)
        self.assertEqual(event.direction, EventDirection.UNCERTAIN)

    def test_rejects_confidence_out_of_range(self):
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            CorporateEvent(
                event_id="event-1",
                event_type="news",
                summary="A news event",
                source_name="Example",
                source_url="https://example.invalid/news",
                published_at=dt(10),
                retrieved_at=dt(10, 1),
                confidence=1.1,
            )


class SwingConfigTests(unittest.TestCase):
    def test_defaults_match_approved_swing_scope(self):
        config = SwingTradingConfig()
        self.assertEqual(config.universe, AnalysisUniverse.NIFTY_50)
        self.assertEqual(
            config.core_timeframes,
            (SwingTimeframe.DAILY, SwingTimeframe.FOUR_HOUR, SwingTimeframe.HOURLY),
        )
        self.assertEqual(config.entry_refinement_timeframe, SwingTimeframe.FIFTEEN_MINUTE)
        self.assertFalse(config.live_trading_enabled)
        self.assertTrue(config.use_closed_candles_only)

    def test_rejects_live_trading(self):
        with self.assertRaisesRegex(ValueError, "must remain disabled"):
            SwingTradingConfig(live_trading_enabled=True)

    def test_rejects_missing_core_timeframe(self):
        with self.assertRaisesRegex(ValueError, "must include 1d, 4h and 1h"):
            SwingTradingConfig(core_timeframes=(SwingTimeframe.DAILY, SwingTimeframe.HOURLY))


if __name__ == "__main__":
    unittest.main()

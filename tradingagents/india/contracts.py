"""Provider-neutral contracts for Indian-market swing research.

These dataclasses validate the shape and basic invariants of data crossing
provider, analytics and agent boundaries. They do not fetch data, infer missing
values, or place orders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Iterable
from zoneinfo import ZoneInfo

INDIA_TIMEZONE = ZoneInfo("Asia/Kolkata")


class SwingTimeframe(str, Enum):
    """Timeframes used by the initial swing-trading strategy."""

    DAILY = "1d"
    FOUR_HOUR = "4h"
    HOURLY = "1h"
    FIFTEEN_MINUTE = "15m"


class AnalysisUniverse(str, Enum):
    """Configurable universe stages; membership itself is data-driven."""

    NIFTY_50 = "nifty50"
    NIFTY_100 = "nifty100"
    NIFTY_500 = "nifty500"
    NSE_ELIGIBLE = "nse_eligible"


class FinancialScope(str, Enum):
    STANDALONE = "standalone"
    CONSOLIDATED = "consolidated"
    UNSPECIFIED = "unspecified"


class EventSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class EventDirection(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _finite_number(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    if not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")


@dataclass(frozen=True, slots=True)
class IndexMembership:
    """Point-in-time membership of an instrument in an index."""

    index_name: str
    valid_from: date
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if not self.index_name.strip():
            raise ValueError("index_name must not be empty")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to cannot precede valid_from")

    def contains(self, on_date: date) -> bool:
        return self.valid_from <= on_date and (self.valid_to is None or on_date <= self.valid_to)


@dataclass(frozen=True, slots=True)
class Instrument:
    """Canonical company/instrument identity independent of any vendor symbol."""

    symbol: str
    exchange: str = "NSE"
    company_name: str | None = None
    instrument_id: str | None = None
    isin: str | None = None
    sector: str | None = None
    industry: str | None = None
    index_memberships: tuple[IndexMembership, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip():
            raise ValueError("symbol must be non-empty and have no surrounding whitespace")
        if not self.exchange or self.exchange != self.exchange.strip():
            raise ValueError("exchange must be non-empty and have no surrounding whitespace")
        if len({(m.index_name, m.valid_from, m.valid_to) for m in self.index_memberships}) != len(
            self.index_memberships
        ):
            raise ValueError("index_memberships contains duplicates")


@dataclass(frozen=True, slots=True)
class OHLCVCandle:
    """A timestamped OHLCV candle with explicit source and completion status."""

    symbol: str
    timeframe: SwingTimeframe
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    source_provider: str
    retrieved_at: datetime
    is_complete: bool
    is_adjusted: bool | None = None
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        if not self.source_provider.strip():
            raise ValueError("source_provider must not be empty")
        _require_aware(self.open_time, "open_time")
        _require_aware(self.close_time, "close_time")
        _require_aware(self.retrieved_at, "retrieved_at")
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        for name in ("open", "high", "low", "close", "volume"):
            _finite_number(getattr(self, name), name)
        if self.volume < 0:
            raise ValueError("volume cannot be negative")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be >= open, close and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be <= open, close and high")
        if not isinstance(self.is_complete, bool):
            raise ValueError("is_complete must be a boolean")


@dataclass(frozen=True, slots=True)
class FinancialFact:
    """A point-in-time financial fact from a published source document."""

    symbol: str
    metric_code: str
    value: float
    unit: str
    currency: str | None
    period_start: date
    period_end: date
    period_type: str
    scope: FinancialScope
    source_name: str
    source_url: str
    published_at: datetime
    retrieved_at: datetime
    filing_status: str = "original"
    revision_of: str | None = None
    reported_metric_name: str | None = None

    def __post_init__(self) -> None:
        for name in ("symbol", "metric_code", "unit", "period_type", "source_name", "source_url"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
        _finite_number(self.value, "value")
        if self.period_end < self.period_start:
            raise ValueError("period_end cannot precede period_start")
        _require_aware(self.published_at, "published_at")
        _require_aware(self.retrieved_at, "retrieved_at")
        if self.filing_status not in {"original", "revised", "restated", "unknown"}:
            raise ValueError("filing_status must be original, revised, restated or unknown")
        if self.filing_status in {"revised", "restated"} and not self.revision_of:
            raise ValueError("revised/restated facts must identify revision_of")


def available_at(fact: FinancialFact, as_of: datetime) -> bool:
    """Return whether a fact was publicly available at the requested time.

    This helper deliberately uses publication time, not period end or retrieval
    time, to prevent financial look-ahead in historical analysis.
    """
    _require_aware(as_of, "as_of")
    return fact.published_at <= as_of


@dataclass(frozen=True, slots=True)
class CorporateEvent:
    """A sourced event with explicit timing and a non-binding impact assessment."""

    event_id: str
    event_type: str
    summary: str
    source_name: str
    source_url: str
    published_at: datetime
    retrieved_at: datetime
    severity: EventSeverity = EventSeverity.UNKNOWN
    direction: EventDirection = EventDirection.UNCERTAIN
    confidence: float = 0.0
    affected_symbols: tuple[str, ...] = field(default_factory=tuple)
    affected_sectors: tuple[str, ...] = field(default_factory=tuple)
    effective_at: datetime | None = None
    is_official_source: bool = False

    def __post_init__(self) -> None:
        for name in ("event_id", "event_type", "summary", "source_name", "source_url"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        _require_aware(self.published_at, "published_at")
        _require_aware(self.retrieved_at, "retrieved_at")
        if self.effective_at is not None:
            _require_aware(self.effective_at, "effective_at")
        _finite_number(self.confidence, "confidence")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if len(set(self.affected_symbols)) != len(self.affected_symbols):
            raise ValueError("affected_symbols contains duplicates")


@dataclass(frozen=True, slots=True)
class SwingTradingConfig:
    """Provider-neutral defaults for the first Indian swing-trading iteration."""

    universe: AnalysisUniverse = AnalysisUniverse.NIFTY_50
    core_timeframes: tuple[SwingTimeframe, ...] = (
        SwingTimeframe.DAILY,
        SwingTimeframe.FOUR_HOUR,
        SwingTimeframe.HOURLY,
    )
    entry_refinement_timeframe: SwingTimeframe | None = SwingTimeframe.FIFTEEN_MINUTE
    timezone: str = "Asia/Kolkata"
    primary_market_provider: str = "fyers"
    secondary_market_provider: str | None = "kite"
    live_trading_enabled: bool = False
    use_closed_candles_only: bool = True

    def __post_init__(self) -> None:
        if self.timezone != "Asia/Kolkata":
            raise ValueError("India swing-trading timezone must be Asia/Kolkata")
        ZoneInfo(self.timezone)  # fail early if timezone database lacks this zone
        required = {SwingTimeframe.DAILY, SwingTimeframe.FOUR_HOUR, SwingTimeframe.HOURLY}
        if not required.issubset(set(self.core_timeframes)):
            raise ValueError("core_timeframes must include 1d, 4h and 1h")
        if len(set(self.core_timeframes)) != len(self.core_timeframes):
            raise ValueError("core_timeframes contains duplicates")
        if not self.primary_market_provider.strip():
            raise ValueError("primary_market_provider must not be empty")
        if self.secondary_market_provider == self.primary_market_provider:
            raise ValueError("primary and secondary market providers must differ")
        if self.live_trading_enabled:
            raise ValueError("live trading is out of scope and must remain disabled")
        if not self.use_closed_candles_only:
            raise ValueError("confirmed swing signals must use closed candles only")


def facts_available_as_of(
    facts: Iterable[FinancialFact], as_of: datetime
) -> tuple[FinancialFact, ...]:
    """Filter facts to those published by an analysis timestamp."""
    _require_aware(as_of, "as_of")
    return tuple(fact for fact in facts if available_at(fact, as_of))

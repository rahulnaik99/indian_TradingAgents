"""India-market domain contracts for the TradingAgents extension.

This package contains provider-neutral contracts, the configurable universe registry,
NSE calendar, FYERS market-data/order adapters, technical analytics, web research,
financial-fact ingestion, paper trading and the integrated research workflow.
"""

from tradingagents.india.exchange_calendar import (
    CalendarCoverageError,
    DEFAULT_NSE_EQUITY_CALENDAR,
    NSEEquityCalendar,
    SessionInfo,
    SessionScheduleUnavailable,
)
from tradingagents.india.universe import (
    DEFAULT_NIFTY50_REGISTRY,
    StockUniverseRegistry,
    UniverseHistoryUnavailable,
    UniverseRegistryError,
    UniverseSnapshot,
)
from tradingagents.india.providers.fyers import (
    FyersAPIError,
    FyersAuthenticationError,
    FyersConfigurationError,
    FyersDataError,
    FyersMarketDataClient,
    FyersRateLimitError,
    FyersRateLimits,
    aggregate_hourly_to_four_hour,
)
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
)

__all__ = [
    "AnalysisUniverse",
    "FyersAPIError",
    "FyersAuthenticationError",
    "FyersConfigurationError",
    "FyersDataError",
    "FyersMarketDataClient",
    "FyersRateLimitError",
    "FyersRateLimits",
    "aggregate_hourly_to_four_hour",
    "CalendarCoverageError",
    "DEFAULT_NSE_EQUITY_CALENDAR",
    "DEFAULT_NIFTY50_REGISTRY",
    "NSEEquityCalendar",
    "SessionInfo",
    "SessionScheduleUnavailable",
    "StockUniverseRegistry",
    "UniverseHistoryUnavailable",
    "UniverseRegistryError",
    "UniverseSnapshot",
    "CorporateEvent",
    "EventDirection",
    "EventSeverity",
    "FinancialFact",
    "FinancialScope",
    "IndexMembership",
    "Instrument",
    "OHLCVCandle",
    "SwingTimeframe",
    "SwingTradingConfig",
    "available_at",
]

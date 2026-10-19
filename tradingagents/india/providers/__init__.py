"""India market-data and gated order-provider adapters."""

from tradingagents.india.providers.fyers_orders import FyersOrderClient, FyersOrderError, LiveTradingDisabled
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

__all__ = [
    "FyersOrderClient",
    "FyersOrderError",
    "LiveTradingDisabled",
    "FyersAPIError",
    "FyersAuthenticationError",
    "FyersConfigurationError",
    "FyersDataError",
    "FyersMarketDataClient",
    "FyersRateLimitError",
    "FyersRateLimits",
    "aggregate_hourly_to_four_hour",
]

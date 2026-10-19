"""Read-only FYERS v3 historical market-data adapter.

This adapter deliberately uses the documented REST history endpoint directly
rather than adding the FYERS SDK as a mandatory dependency. It does not expose
any order-placement methods.

Authentication uses a user-generated FYERS access token supplied via the
FYERS_APP_ID and FYERS_ACCESS_TOKEN environment variables. Tokens and secrets
must never be committed to source control or pasted into logs.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import requests

from tradingagents.india.contracts import INDIA_TIMEZONE, OHLCVCandle, SwingTimeframe

FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"
_SYMBOL_RE = re.compile(r"^[A-Z0-9&_-]+$")


class FyersError(RuntimeError):
    """Base class for adapter errors safe to show without exposing credentials."""


class FyersConfigurationError(FyersError):
    """Required configuration is missing or invalid."""


class FyersAuthenticationError(FyersError):
    """FYERS rejected authentication or the token lacks access."""


class FyersRateLimitError(FyersError):
    """The configured local request budget or FYERS quota was exhausted."""


class FyersDataError(FyersError):
    """The provider returned malformed or internally inconsistent data."""


class FyersAPIError(FyersError):
    """A non-authentication provider error."""

    def __init__(self, message: str, *, status_code: int | None = None, provider_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code


@dataclass(frozen=True, slots=True)
class FyersRateLimits:
    """Local conservative request budget; defaults reflect FYERS Standard Data API limits."""

    requests_per_second: int = 5
    requests_per_minute: int = 50
    requests_per_day: int = 5000

    def __post_init__(self) -> None:
        if min(self.requests_per_second, self.requests_per_minute, self.requests_per_day) < 1:
            raise ValueError("all rate limits must be positive")
        if self.requests_per_second > self.requests_per_minute:
            raise ValueError("requests_per_second cannot exceed requests_per_minute")
        if self.requests_per_minute > self.requests_per_day:
            raise ValueError("requests_per_minute cannot exceed requests_per_day")


class _SlidingWindowLimiter:
    """In-process sliding-window limiter; not a cross-process quota coordinator."""

    def __init__(
        self,
        limits: FyersRateLimits,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.limits = limits
        self._monotonic = monotonic
        self._sleep = sleep
        self._wall_clock = wall_clock or (lambda: datetime.now(INDIA_TIMEZONE))
        self._second: deque[float] = deque()
        self._minute: deque[float] = deque()
        self._day_count = 0
        self._day: date | None = None

    def acquire(self) -> None:
        while True:
            now = self._monotonic()
            current_day = self._wall_clock().astimezone(INDIA_TIMEZONE).date()
            if current_day != self._day:
                self._day = current_day
                self._day_count = 0
            if self._day_count >= self.limits.requests_per_day:
                raise FyersRateLimitError(
                    "Local FYERS daily request budget exhausted; resume after the India-local day resets."
                )
            while self._second and now - self._second[0] >= 1.0:
                self._second.popleft()
            while self._minute and now - self._minute[0] >= 60.0:
                self._minute.popleft()

            waits: list[float] = []
            if len(self._second) >= self.limits.requests_per_second:
                waits.append(max(0.001, 1.0 - (now - self._second[0])))
            if len(self._minute) >= self.limits.requests_per_minute:
                waits.append(max(0.001, 60.0 - (now - self._minute[0])))
            if waits:
                self._sleep(max(waits))
                continue

            now = self._monotonic()
            self._second.append(now)
            self._minute.append(now)
            self._day_count += 1
            return


class FyersMarketDataClient:
    """Fetch and normalize read-only FYERS historical candles.

    Args:
        app_id: FYERS app ID (for example, the ID ending in ``-100``).
        access_token: FYERS access token generated through the user's account.
        session: Optional requests-compatible session, mainly for tests.
        max_retries: Retries for network errors, HTTP 429, and HTTP 5xx only.
        limits: Local request budget. FYERS Prime users can pass Prime limits.

    Notes:
        * Daily requests are chunked to at most 366 calendar dates.
        * Intraday requests are chunked to at most 100 calendar dates.
        * 4-hour candles are aggregated from hourly candles on each session,
          anchored at 09:15 IST. The final session bar can be shorter than four
          hours (typically 13:15–15:30) and is flagged accordingly.
        * Provider responses are historical data; this client does not poll for
          live quotes and does not place orders.
    """

    provider_name = "fyers"

    def __init__(
        self,
        app_id: str,
        access_token: str,
        *,
        base_url: str = FYERS_HISTORY_URL,
        session: Any | None = None,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        limits: FyersRateLimits | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not app_id or not app_id.strip():
            raise FyersConfigurationError("FYERS_APP_ID is required")
        if not access_token or not access_token.strip():
            raise FyersConfigurationError("FYERS_ACCESS_TOKEN is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds cannot be negative")
        self.app_id = app_id.strip()
        self._access_token = access_token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._session = session or requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic
        self._now = now or (lambda: datetime.now(INDIA_TIMEZONE))
        self._limiter = _SlidingWindowLimiter(
            limits or FyersRateLimits(), monotonic=monotonic, sleep=sleep, wall_clock=self._now
        )

    @classmethod
    def from_env(cls, **kwargs: Any) -> "FyersMarketDataClient":
        """Create the adapter from environment variables without loading files implicitly."""
        base_url = kwargs.pop("base_url", os.getenv("FYERS_DATA_BASE_URL", FYERS_HISTORY_URL))
        limits = kwargs.pop("limits", None)
        if limits is None:
            try:
                limits = FyersRateLimits(
                    requests_per_second=int(os.getenv("FYERS_REQUESTS_PER_SECOND", "5")),
                    requests_per_minute=int(os.getenv("FYERS_REQUESTS_PER_MINUTE", "50")),
                    requests_per_day=int(os.getenv("FYERS_REQUESTS_PER_DAY", "5000")),
                )
            except ValueError as exc:
                raise FyersConfigurationError("FYERS request-limit environment variables must be integers") from exc
        return cls(
            os.getenv("FYERS_APP_ID", ""),
            os.getenv("FYERS_ACCESS_TOKEN", ""),
            base_url=base_url,
            limits=limits,
            **kwargs,
        )

    @staticmethod
    def vendor_symbol(symbol: str) -> str:
        """Map a canonical NSE equity symbol to FYERS format."""
        cleaned = symbol.strip().upper()
        if cleaned.startswith("NSE:") and cleaned.endswith("-EQ"):
            body = cleaned[4:-3]
            if not body or not _SYMBOL_RE.fullmatch(body):
                raise ValueError(f"Invalid FYERS equity symbol: {symbol!r}")
            return cleaned
        if not _SYMBOL_RE.fullmatch(cleaned):
            raise ValueError(f"Invalid canonical NSE symbol: {symbol!r}")
        return f"NSE:{cleaned}-EQ"

    @staticmethod
    def _normalize_timeframe(timeframe: SwingTimeframe | str) -> SwingTimeframe:
        if isinstance(timeframe, SwingTimeframe):
            return timeframe
        try:
            return SwingTimeframe(timeframe.lower())
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"Unsupported swing timeframe: {timeframe!r}") from exc

    def get_history(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        timeframe: SwingTimeframe | str,
    ) -> tuple[OHLCVCandle, ...]:
        """Fetch sorted, de-duplicated candles for an inclusive date range."""
        if not isinstance(start_date, date) or isinstance(start_date, datetime):
            raise TypeError("start_date must be a datetime.date")
        if not isinstance(end_date, date) or isinstance(end_date, datetime):
            raise TypeError("end_date must be a datetime.date")
        if end_date < start_date:
            raise ValueError("end_date cannot precede start_date")
        normalized_tf = self._normalize_timeframe(timeframe)
        vendor_symbol = self.vendor_symbol(symbol)

        if normalized_tf == SwingTimeframe.FOUR_HOUR:
            hourly = self._fetch_native(vendor_symbol, symbol, start_date, end_date, SwingTimeframe.HOURLY)
            return aggregate_hourly_to_four_hour(hourly)
        return self._fetch_native(vendor_symbol, symbol, start_date, end_date, normalized_tf)

    def _fetch_native(
        self,
        vendor_symbol: str,
        canonical_symbol: str,
        start_date: date,
        end_date: date,
        timeframe: SwingTimeframe,
    ) -> tuple[OHLCVCandle, ...]:
        resolution, max_days = {
            SwingTimeframe.DAILY: ("D", 366),
            SwingTimeframe.HOURLY: ("60", 100),
            SwingTimeframe.FIFTEEN_MINUTE: ("15", 100),
            SwingTimeframe.FOUR_HOUR: ("60", 100),  # used only as aggregation input
        }[timeframe]
        candles: dict[datetime, OHLCVCandle] = {}
        cursor = start_date
        while cursor <= end_date:
            chunk_end = min(end_date, cursor + timedelta(days=max_days - 1))
            payload = self._request_history(vendor_symbol, resolution, cursor, chunk_end)
            retrieved_at = self._now()
            for row in payload:
                # FYERS can return pre-market/out-of-session intraday rows for some
                # history ranges. This strategy uses regular NSE cash-session bars
                # only, so discard those rows instead of failing the entire analysis.
                # Malformed timestamps are deliberately passed to _parse_candle so
                # genuine data corruption still fails closed.
                if timeframe != SwingTimeframe.DAILY and not self._is_regular_intraday_row(row):
                    continue
                candle = self._parse_candle(canonical_symbol, timeframe, row, retrieved_at)
                if start_date <= candle.open_time.astimezone(INDIA_TIMEZONE).date() <= end_date:
                    candles[candle.open_time] = candle
            cursor = chunk_end + timedelta(days=1)
        return tuple(candles[key] for key in sorted(candles))

    @staticmethod
    def _is_regular_intraday_row(row: list[Any]) -> bool:
        """Return whether an intraday row opens during regular NSE cash hours.

        NSE cash-session bars are accepted from 09:15 inclusive to 15:30
        exclusive, India time. If the timestamp is malformed, return True so
        _parse_candle raises the adapter's normal, actionable data error.
        """
        try:
            timestamp = int(row[0])
            local_open = datetime.fromtimestamp(timestamp, tz=ZoneInfo("UTC")).astimezone(INDIA_TIMEZONE)
        except (TypeError, ValueError, OverflowError, OSError, IndexError):
            return True
        return wall_time(9, 15) <= local_open.time() < wall_time(15, 30)

    def _request_history(
        self, symbol: str, resolution: str, range_from: date, range_to: date
    ) -> list[list[Any]]:
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": range_from.isoformat(),
            "range_to": range_to.isoformat(),
            "cont_flag": "0",
        }
        headers = {
            "Authorization": f"{self.app_id}:{self._access_token}",
            "Accept": "application/json",
            "User-Agent": "TradingAgents-India/0.1 (read-only market research)",
        }
        for attempt in range(self.max_retries + 1):
            self._limiter.acquire()
            try:
                response = self._session.get(
                    self.base_url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise FyersAPIError(f"FYERS history request failed: {type(exc).__name__}") from exc
                self._sleep(self.retry_backoff_seconds * (2**attempt))
                continue

            status_code = int(getattr(response, "status_code", 200))
            if status_code in (401, 403):
                raise FyersAuthenticationError(
                    "FYERS rejected the request. Check that the access token is current and the app has Market Data permission."
                )
            if status_code == 429:
                if attempt >= self.max_retries:
                    raise FyersRateLimitError("FYERS returned HTTP 429 after retry attempts")
                self._sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            if status_code >= 500:
                if attempt >= self.max_retries:
                    raise FyersAPIError(f"FYERS server error (HTTP {status_code})", status_code=status_code)
                self._sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            if status_code >= 400:
                raise FyersAPIError(f"FYERS rejected the request (HTTP {status_code})", status_code=status_code)
            try:
                body = response.json()
            except (ValueError, AttributeError) as exc:
                raise FyersDataError("FYERS returned a non-JSON history response") from exc
            if not isinstance(body, Mapping):
                raise FyersDataError("FYERS history response must be a JSON object")
            status = str(body.get("s", "")).lower()
            if status != "ok":
                code = body.get("code")
                try:
                    provider_code = int(code) if code is not None else None
                except (TypeError, ValueError):
                    provider_code = None
                message = str(body.get("message") or body.get("messageCode") or "unknown provider error")
                lowered = message.lower()
                if provider_code in {-8, -15, -16, -17} or any(
                    term in lowered for term in ("token", "auth", "permission")
                ):
                    raise FyersAuthenticationError(f"FYERS authentication/permission error: {message}")
                raise FyersAPIError(
                    f"FYERS history API error: {message}",
                    status_code=status_code,
                    provider_code=provider_code,
                )
            rows = body.get("candles", [])
            if not isinstance(rows, list):
                raise FyersDataError("FYERS 'candles' field must be a list")
            if any(not isinstance(row, (list, tuple)) or len(row) < 6 for row in rows):
                raise FyersDataError("FYERS candle rows must contain timestamp, OHLC and volume")
            return rows
        raise AssertionError("unreachable retry loop")

    def _parse_candle(
        self,
        canonical_symbol: str,
        timeframe: SwingTimeframe,
        row: list[Any],
        retrieved_at: datetime,
    ) -> OHLCVCandle:
        try:
            timestamp = int(row[0])
            open_price, high, low, close, volume = (float(row[i]) for i in range(1, 6))
            source_open = datetime.fromtimestamp(timestamp, tz=ZoneInfo("UTC")).astimezone(INDIA_TIMEZONE)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise FyersDataError("FYERS returned a candle with invalid timestamp or numeric fields") from exc
        if not all(math.isfinite(value) for value in (open_price, high, low, close, volume)):
            raise FyersDataError("FYERS returned non-finite OHLCV values")

        if timeframe == SwingTimeframe.DAILY:
            session_date = source_open.date()
            open_time = datetime.combine(session_date, wall_time(9, 15), tzinfo=INDIA_TIMEZONE)
            close_time = datetime.combine(session_date, wall_time(15, 30), tzinfo=INDIA_TIMEZONE)
        else:
            open_time = source_open
            minutes = {
                SwingTimeframe.HOURLY: 60,
                SwingTimeframe.FIFTEEN_MINUTE: 15,
                SwingTimeframe.FOUR_HOUR: 240,
            }[timeframe]
            theoretical_close = open_time + timedelta(minutes=minutes)
            session_close = datetime.combine(open_time.date(), wall_time(15, 30), tzinfo=INDIA_TIMEZONE)
            close_time = min(theoretical_close, session_close)
            if open_time.time() < wall_time(9, 15) or open_time.time() >= wall_time(15, 30):
                raise FyersDataError(f"FYERS intraday candle timestamp outside regular NSE session: {open_time!s}")

        if close_time <= open_time:
            raise FyersDataError("FYERS candle has a non-positive interval")
        complete = retrieved_at.astimezone(INDIA_TIMEZONE) >= close_time
        return OHLCVCandle(
            symbol=canonical_symbol,
            timeframe=timeframe,
            open_time=open_time,
            close_time=close_time,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            source_provider=self.provider_name,
            retrieved_at=retrieved_at,
            is_complete=complete,
            is_adjusted=None,
            quality_flags=() if complete else ("in_progress_or_unclosed_candle",),
        )


def aggregate_hourly_to_four_hour(candles: tuple[OHLCVCandle, ...] | list[OHLCVCandle]) -> tuple[OHLCVCandle, ...]:
    """Aggregate hourly candles into session-anchored 4H candles.

    Each session starts at 09:15 IST. Four-hour groups are anchored at 09:15;
    the last group is allowed to be shorter than four hours because NSE's regular
    cash session is 6h15m. Only supplied candles are aggregated; no bars are
    fabricated to fill gaps.
    """
    ordered = sorted(candles, key=lambda candle: candle.open_time)
    groups: dict[tuple[str, date, int], list[OHLCVCandle]] = {}
    for candle in ordered:
        if candle.timeframe != SwingTimeframe.HOURLY:
            raise ValueError("4H aggregation input must contain hourly candles only")
        local_open = candle.open_time.astimezone(INDIA_TIMEZONE)
        session_open = datetime.combine(local_open.date(), wall_time(9, 15), tzinfo=INDIA_TIMEZONE)
        elapsed_seconds = (local_open - session_open).total_seconds()
        if elapsed_seconds < 0 or local_open.time() >= wall_time(15, 30):
            raise ValueError(f"Hourly candle falls outside regular NSE session: {local_open!s}")
        group_index = int(elapsed_seconds // (4 * 60 * 60))
        groups.setdefault((candle.symbol, local_open.date(), group_index), []).append(candle)

    result: list[OHLCVCandle] = []
    for (symbol, session_date, group_index), group in sorted(groups.items()):
        group.sort(key=lambda candle: candle.open_time)
        open_time = group[0].open_time
        close_time = group[-1].close_time
        full_span = (close_time - open_time).total_seconds() >= 4 * 60 * 60
        flags = sorted({flag for candle in group for flag in candle.quality_flags})
        if not full_span:
            flags.append("partial_session_bar")
        expected_starts = 4 if group_index == 0 else 3
        if len(group) < expected_starts:
            flags.append("missing_hourly_candle")
        for previous, current in zip(group, group[1:]):
            if (current.open_time - previous.open_time).total_seconds() > 60 * 60:
                flags.append("missing_hourly_candle")
                break
        complete = all(candle.is_complete for candle in group)
        if not complete and "in_progress_or_unclosed_candle" not in flags:
            flags.append("in_progress_or_unclosed_candle")
        result.append(
            OHLCVCandle(
                symbol=symbol,
                timeframe=SwingTimeframe.FOUR_HOUR,
                open_time=open_time,
                close_time=close_time,
                open=group[0].open,
                high=max(candle.high for candle in group),
                low=min(candle.low for candle in group),
                close=group[-1].close,
                volume=sum(candle.volume for candle in group),
                source_provider="fyers",
                retrieved_at=max(candle.retrieved_at for candle in group),
                is_complete=complete,
                is_adjusted=None,
                quality_flags=tuple(flags),
            )
        )
    return tuple(result)

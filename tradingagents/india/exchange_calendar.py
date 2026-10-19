"""NSE cash-equity session calendar with explicit holiday coverage.

The calendar intentionally fails closed outside configured years. Update its
CSV from official NSE circulars before using new calendar years or historical
periods not covered by the checked-in file.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

INDIA_TZ = ZoneInfo("Asia/Kolkata")
REGULAR_OPEN = time(9, 15)
REGULAR_CLOSE = time(15, 30)


class CalendarCoverageError(ValueError):
    """Raised when a date's exchange calendar has not been configured."""


class SessionScheduleUnavailable(CalendarCoverageError):
    """Raised when a special trading session exists but its hours are unknown."""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_date: date
    session_type: str
    open_time: time | None
    close_time: time | None
    schedule_confirmed: bool

    def open_datetime(self) -> datetime | None:
        if self.open_time is None:
            return None
        return datetime.combine(self.session_date, self.open_time, tzinfo=INDIA_TZ)

    def close_datetime(self) -> datetime | None:
        if self.close_time is None:
            return None
        return datetime.combine(self.session_date, self.close_time, tzinfo=INDIA_TZ)


@dataclass(frozen=True, slots=True)
class HolidayRecord:
    holiday_date: date
    name: str
    is_trading_holiday: bool = True
    special_session: bool = False
    special_open: time | None = None
    special_close: time | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("holiday name must not be empty")
        if self.special_open is not None and self.special_close is not None:
            if self.special_close <= self.special_open:
                raise ValueError("special_close must be after special_open")
        if (self.special_open is None) != (self.special_close is None):
            raise ValueError("special session must specify both open and close times")


class NSEEquityCalendar:
    """Session rules for the NSE equity regular session and configured exceptions."""

    def __init__(self, records: list[HolidayRecord] | tuple[HolidayRecord, ...]) -> None:
        self._records: dict[date, HolidayRecord] = {}
        self._covered_years: set[int] = set()
        for record in records:
            if record.holiday_date in self._records:
                raise ValueError(f"duplicate calendar record for {record.holiday_date}")
            self._records[record.holiday_date] = record
            self._covered_years.add(record.holiday_date.year)
        if not self._covered_years:
            raise ValueError("calendar must contain records for at least one year")
        # A year is considered covered only when its source file declares the
        # coverage year. The official holiday CSV uses one row per holiday, so
        # coverage_years can be explicitly supplied by from_csv.

    @classmethod
    def from_csv(cls, path: str | Path) -> "NSEEquityCalendar":
        csv_path = Path(path)
        if not csv_path.is_file():
            raise FileNotFoundError(csv_path)
        records: list[HolidayRecord] = []
        declared_years: set[int] = set()
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"date", "name", "is_trading_holiday", "special_session"}
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise ValueError("calendar CSV missing columns: " + ", ".join(sorted(missing)))
            for line_number, row in enumerate(reader, start=2):
                try:
                    holiday_date = date.fromisoformat((row.get("date") or "").strip())
                    declared_years.add(holiday_date.year)
                    is_holiday = _parse_bool(row.get("is_trading_holiday", "true"))
                    is_special = _parse_bool(row.get("special_session", "false"))
                    open_text = (row.get("special_open") or "").strip()
                    close_text = (row.get("special_close") or "").strip()
                    records.append(
                        HolidayRecord(
                            holiday_date=holiday_date,
                            name=(row.get("name") or "").strip(),
                            is_trading_holiday=is_holiday,
                            special_session=is_special,
                            special_open=time.fromisoformat(open_text) if open_text else None,
                            special_close=time.fromisoformat(close_text) if close_text else None,
                        )
                    )
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"invalid calendar CSV row {line_number}: {exc}") from exc
        calendar = cls(records)
        # Calendar coverage is explicitly declared; this avoids assuming a
        # holiday list for one year proves completeness for adjacent years.
        calendar._covered_years = declared_years
        return calendar

    @classmethod
    def builtin(cls) -> "NSEEquityCalendar":
        data_path = Path(__file__).parent / "data" / "nse_equity_holidays_2026.csv"
        return cls.from_csv(data_path)

    @property
    def covered_years(self) -> tuple[int, ...]:
        return tuple(sorted(self._covered_years))

    def ensure_coverage(self, day: date) -> None:
        if day.year not in self._covered_years:
            raise CalendarCoverageError(
                f"NSE equity holiday coverage is not configured for {day.year}; "
                "update the official holiday calendar before analysis"
            )

    def record_for(self, day: date) -> HolidayRecord | None:
        self.ensure_coverage(day)
        return self._records.get(day)

    def is_trading_day(self, day: date) -> bool:
        record = self.record_for(day)
        if record and record.special_session:
            # NSE may declare a special session on a date that is otherwise a
            # holiday/weekend. It is a trading day even if hours are not yet known.
            return True
        if day.weekday() >= 5:
            return False
        return not (record and record.is_trading_holiday)

    def session_for(self, day: date) -> SessionInfo | None:
        record = self.record_for(day)
        if record and record.special_session:
            return SessionInfo(
                session_date=day,
                session_type="special",
                open_time=record.special_open,
                close_time=record.special_close,
                schedule_confirmed=record.special_open is not None,
            )
        if day.weekday() >= 5 or (record and record.is_trading_holiday):
            return None
        return SessionInfo(
            session_date=day,
            session_type="regular",
            open_time=REGULAR_OPEN,
            close_time=REGULAR_CLOSE,
            schedule_confirmed=True,
        )

    def is_regular_session_day(self, day: date) -> bool:
        session = self.session_for(day)
        return bool(session and session.session_type == "regular")

    def is_market_open(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at must be timezone-aware")
        local_at = at.astimezone(INDIA_TZ)
        session = self.session_for(local_at.date())
        if session is None:
            return False
        if not session.schedule_confirmed:
            raise SessionScheduleUnavailable(
                f"special session hours are not yet published for {local_at.date()}"
            )
        assert session.open_time is not None and session.close_time is not None
        return session.open_time <= local_at.time().replace(tzinfo=None) < session.close_time

    def next_trading_day(self, day: date, include_day: bool = False) -> date:
        candidate = day if include_day else day + timedelta(days=1)
        # Bound the loop to avoid an infinite loop on malformed/unsupported data.
        for _ in range(370):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        raise CalendarCoverageError("could not find next trading day within calendar coverage")

    def previous_trading_day(self, day: date, include_day: bool = False) -> date:
        candidate = day if include_day else day - timedelta(days=1)
        for _ in range(370):
            if self.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise CalendarCoverageError("could not find previous trading day within calendar coverage")


def _parse_bool(value: str | None) -> bool:
    normalized = (value or "").strip().casefold()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


DEFAULT_NSE_EQUITY_CALENDAR = NSEEquityCalendar.builtin()

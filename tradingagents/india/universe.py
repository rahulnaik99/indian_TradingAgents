"""Configurable stock universe registry with dated constituent snapshots.

No network requests are performed. Import official, dated CSV snapshots into
this registry so historical membership is explicit rather than inferred.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from tradingagents.india.contracts import Instrument


class UniverseRegistryError(ValueError):
    """Base exception for malformed or unavailable universe data."""


class UniverseHistoryUnavailable(UniverseRegistryError):
    """Raised when no constituent snapshot exists on or before a requested date."""


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    index_name: str
    snapshot_date: date
    instruments: tuple[Instrument, ...]
    source_name: str
    source_url: str

    def __post_init__(self) -> None:
        if not self.index_name.strip():
            raise UniverseRegistryError("index_name must not be empty")
        if not self.source_name.strip() or not self.source_url.strip():
            raise UniverseRegistryError("snapshot source name and URL are required")
        if not self.instruments:
            raise UniverseRegistryError("snapshot must contain at least one instrument")
        symbols = [instrument.symbol for instrument in self.instruments]
        if len(symbols) != len(set(symbols)):
            raise UniverseRegistryError("snapshot contains duplicate symbols")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(instrument.symbol for instrument in self.instruments)


class StockUniverseRegistry:
    """In-memory registry populated from versioned CSV snapshots."""

    REQUIRED_COLUMNS = {
        "index_name",
        "snapshot_date",
        "source_name",
        "source_url",
        "symbol",
        "company_name",
    }

    def __init__(self, snapshots: Iterable[UniverseSnapshot] = ()) -> None:
        self._snapshots: dict[str, list[UniverseSnapshot]] = {}
        for snapshot in snapshots:
            self.add_snapshot(snapshot)

    def add_snapshot(self, snapshot: UniverseSnapshot) -> None:
        index_key = snapshot.index_name.strip().casefold()
        snapshots = self._snapshots.setdefault(index_key, [])
        if any(existing.snapshot_date == snapshot.snapshot_date for existing in snapshots):
            raise UniverseRegistryError(
                f"duplicate snapshot for {snapshot.index_name} on {snapshot.snapshot_date}"
            )
        snapshots.append(snapshot)
        snapshots.sort(key=lambda item: item.snapshot_date)

    @classmethod
    def from_csv(cls, path: str | Path) -> "StockUniverseRegistry":
        """Load one or more dated index snapshots from a CSV file.

        CSV columns: index_name,snapshot_date,source_name,source_url,symbol,
        company_name,exchange,isin,sector,industry. One row per symbol per
        snapshot. Multiple snapshots may be stored in the same file.
        """
        csv_path = Path(path)
        if not csv_path.is_file():
            raise FileNotFoundError(csv_path)

        grouped: dict[tuple[str, date, str, str], list[Instrument]] = {}
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            missing = cls.REQUIRED_COLUMNS - set(reader.fieldnames or ())
            if missing:
                raise UniverseRegistryError(
                    "CSV missing required columns: " + ", ".join(sorted(missing))
                )
            for line_number, row in enumerate(reader, start=2):
                try:
                    index_name = (row.get("index_name") or "").strip()
                    snapshot_date = date.fromisoformat((row.get("snapshot_date") or "").strip())
                    source_name = (row.get("source_name") or "").strip()
                    source_url = (row.get("source_url") or "").strip()
                    symbol = (row.get("symbol") or "").strip().upper()
                    company_name = (row.get("company_name") or "").strip()
                    if not all((index_name, source_name, source_url, symbol, company_name)):
                        raise ValueError("required text field is empty")
                    key = (index_name, snapshot_date, source_name, source_url)
                    instrument = Instrument(
                        symbol=symbol,
                        exchange=(row.get("exchange") or "NSE").strip() or "NSE",
                        company_name=company_name,
                        isin=(row.get("isin") or "").strip() or None,
                        sector=(row.get("sector") or "").strip() or None,
                        industry=(row.get("industry") or "").strip() or None,
                    )
                    grouped.setdefault(key, []).append(instrument)
                except (TypeError, ValueError) as exc:
                    raise UniverseRegistryError(
                        f"invalid CSV row {line_number}: {exc}"
                    ) from exc

        snapshots = [
            UniverseSnapshot(
                index_name=key[0],
                snapshot_date=key[1],
                source_name=key[2],
                source_url=key[3],
                instruments=tuple(instruments),
            )
            for key, instruments in grouped.items()
        ]
        return cls(snapshots)

    @classmethod
    def from_nse_index_csv(
        cls,
        path: str | Path,
        *,
        index_name: str,
        snapshot_date: date,
        source_url: str = "https://www.nseindia.com/products-services/indices-niftyindices",
        source_name: str = "NSE Indices - official constituent CSV",
    ) -> "StockUniverseRegistry":
        """Load an official NSE/Nifty Indices constituent CSV (e.g. ind_nifty500list.csv).

        Expected columns: Company Name, Industry, Symbol, Series, ISIN Code.
        The caller supplies the index name and the effective date, because the
        file itself carries neither.
        """
        csv_path = Path(path)
        if not csv_path.is_file():
            raise FileNotFoundError(csv_path)
        instruments: list[Instrument] = []
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            columns = {(name or "").strip().casefold(): name for name in (reader.fieldnames or ())}
            if "symbol" not in columns or "company name" not in columns:
                raise UniverseRegistryError("NSE CSV must contain 'Company Name' and 'Symbol' columns")
            for line_number, row in enumerate(reader, start=2):
                symbol = (row.get(columns["symbol"]) or "").strip().upper()
                company = (row.get(columns["company name"]) or "").strip()
                if not symbol and not company:
                    continue
                if not symbol or not company:
                    raise UniverseRegistryError(f"invalid NSE CSV row {line_number}: symbol/company missing")
                if symbol.startswith("DUMMY"):  # NSE placeholder rows for pending listings
                    continue
                isin_col, industry_col = columns.get("isin code"), columns.get("industry")
                instruments.append(Instrument(
                    symbol=symbol, exchange="NSE", company_name=company,
                    isin=((row.get(isin_col) or "").strip() or None) if isin_col else None,
                    sector=None,
                    industry=((row.get(industry_col) or "").strip() or None) if industry_col else None,
                ))
        return cls([UniverseSnapshot(
            index_name=index_name, snapshot_date=snapshot_date, instruments=tuple(instruments),
            source_name=source_name, source_url=source_url,
        )])

    @classmethod
    def builtin(cls) -> "StockUniverseRegistry":
        """Load the checked-in, explicitly dated NIFTY 50 constituent snapshot."""
        data_path = Path(__file__).parent / "data" / "nifty50_constituents.csv"
        return cls.from_csv(data_path)

    def index_names(self) -> tuple[str, ...]:
        return tuple(sorted(snapshot_list[0].index_name for snapshot_list in self._snapshots.values()))

    def snapshots(self, index_name: str) -> tuple[UniverseSnapshot, ...]:
        return tuple(self._snapshots.get(index_name.strip().casefold(), ()))

    def get_snapshot(self, index_name: str, as_of: date | None = None) -> UniverseSnapshot:
        """Return the latest available snapshot no later than ``as_of``.

        If no snapshot is old enough, raise instead of silently treating today's
        constituents as historical truth. A snapshot is only as reliable as its
        effective date and source metadata.
        """
        available = self.snapshots(index_name)
        if as_of is not None:
            available = tuple(item for item in available if item.snapshot_date <= as_of)
        if not available:
            raise UniverseHistoryUnavailable(
                f"no {index_name!r} constituent snapshot available on or before {as_of}"
            )
        return available[-1]

    def members(self, index_name: str, as_of: date | None = None) -> tuple[Instrument, ...]:
        return self.get_snapshot(index_name, as_of).instruments

    def symbols(self, index_name: str, as_of: date | None = None) -> tuple[str, ...]:
        return self.get_snapshot(index_name, as_of).symbols


DEFAULT_NIFTY50_REGISTRY = StockUniverseRegistry.builtin()

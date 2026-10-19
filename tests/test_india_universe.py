"""Tests for dated constituent snapshots and configurable stock universes."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from tradingagents.india.universe import (
    StockUniverseRegistry,
    UniverseHistoryUnavailable,
    UniverseRegistryError,
    UniverseSnapshot,
)
from tradingagents.india.contracts import Instrument


class BuiltinUniverseTests(unittest.TestCase):
    def test_builtin_nifty50_snapshot_has_50_unique_symbols(self):
        registry = StockUniverseRegistry.builtin()
        snapshot = registry.get_snapshot("NIFTY 50")
        self.assertEqual(len(snapshot.instruments), 50)
        self.assertEqual(len(set(snapshot.symbols)), 50)
        self.assertEqual(snapshot.snapshot_date, date(2026, 9, 30))
        self.assertIn("BSE", snapshot.symbols)
        self.assertNotIn("WIPRO", snapshot.symbols)

    def test_snapshot_is_not_assumed_to_cover_earlier_history(self):
        registry = StockUniverseRegistry.builtin()
        with self.assertRaises(UniverseHistoryUnavailable):
            registry.get_snapshot("NIFTY 50", date(2026, 9, 29))

    def test_members_are_case_insensitive_by_index_name(self):
        registry = StockUniverseRegistry.builtin()
        self.assertEqual(len(registry.members("nifty 50", date(2026, 10, 9))), 50)

    def test_duplicate_snapshot_date_is_rejected(self):
        instrument = Instrument(symbol="ABC", company_name="ABC Ltd")
        snapshot = UniverseSnapshot(
            index_name="Example", snapshot_date=date(2026, 1, 1),
            instruments=(instrument,), source_name="Fixture", source_url="https://example.invalid",
        )
        registry = StockUniverseRegistry((snapshot,))
        with self.assertRaises(UniverseRegistryError):
            registry.add_snapshot(snapshot)

    def test_csv_loader_supports_multiple_snapshots(self):
        rows = [
            "index_name,snapshot_date,source_name,source_url,symbol,company_name,exchange,isin,sector,industry",
            "Example,2026-01-01,Fixture,https://example.invalid,AAA,AAA Ltd,NSE,,,,",
            "Example,2026-02-01,Fixture,https://example.invalid,BBB,BBB Ltd,NSE,,,,",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "members.csv"
            path.write_text("\n".join(rows), encoding="utf-8")
            registry = StockUniverseRegistry.from_csv(path)
        self.assertEqual(registry.symbols("Example", date(2026, 1, 15)), ("AAA",))
        self.assertEqual(registry.symbols("Example", date(2026, 2, 1)), ("BBB",))


if __name__ == "__main__":
    unittest.main()

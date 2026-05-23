import tempfile
import unittest
from datetime import date
from pathlib import Path

from kronos_mvp.models import Candle
from kronos_mvp.storage import CandleStore


class CandleStoreTests(unittest.TestCase):
    def test_upsert_and_read_latest_candles_in_date_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            store.upsert_many(
                "600519",
                [
                    Candle(date=date(2026, 5, 20), open=10, high=12, low=9, close=11, volume=100, amount=1100),
                    Candle(date=date(2026, 5, 21), open=11, high=13, low=10, close=12, volume=120, amount=1440),
                    Candle(date=date(2026, 5, 19), open=9, high=11, low=8, close=10, volume=90, amount=900),
                ],
            )

            candles = store.get_latest("600519", limit=2)

            self.assertEqual([c.date for c in candles], [date(2026, 5, 20), date(2026, 5, 21)])
            self.assertEqual(candles[-1].close, 12)

    def test_upsert_replaces_existing_symbol_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            store.upsert_many("000001", [Candle(date=date(2026, 5, 20), open=1, high=2, low=1, close=1.5, volume=1, amount=1)])
            store.upsert_many("000001", [Candle(date=date(2026, 5, 20), open=2, high=3, low=2, close=2.5, volume=2, amount=2)])

            candles = store.get_latest("000001", limit=5)

            self.assertEqual(len(candles), 1)
            self.assertEqual(candles[0].close, 2.5)

    def test_merge_from_imports_rows_from_other_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = CandleStore(Path(tmp) / "target.db")
            source = CandleStore(Path(tmp) / "source.db")
            source.upsert_many(
                "600519",
                [Candle(date=date(2026, 5, 20), open=10, high=12, low=9, close=11, volume=100, amount=1100)],
            )

            merged = target.merge_from(source.db_path)

            self.assertEqual(merged, 1)
            candles = target.get_latest("600519", limit=1)
            self.assertEqual(len(candles), 1)
            self.assertEqual(candles[0].close, 11)


if __name__ == "__main__":
    unittest.main()

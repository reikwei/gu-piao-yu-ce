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
                    Candle(date=date(2026, 5, 20), open=10, high=12, low=9, close=11, volume=100, amount=1100, turnover=1.1),
                    Candle(date=date(2026, 5, 21), open=11, high=13, low=10, close=12, volume=120, amount=1440, turnover=1.3),
                    Candle(date=date(2026, 5, 19), open=9, high=11, low=8, close=10, volume=90, amount=900, turnover=0.9),
                ],
            )

            candles = store.get_latest("600519", limit=2)

            self.assertEqual([c.date for c in candles], [date(2026, 5, 20), date(2026, 5, 21)])
            self.assertEqual(candles[-1].close, 12)
            self.assertEqual(candles[-1].turnover, 1.3)

    def test_upsert_replaces_existing_symbol_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            store.upsert_many("000001", [Candle(date=date(2026, 5, 20), open=1, high=2, low=1, close=1.5, volume=1, amount=1)])
            store.upsert_many("000001", [Candle(date=date(2026, 5, 20), open=2, high=3, low=2, close=2.5, volume=2, amount=2)])

            candles = store.get_latest("000001", limit=5)

            self.assertEqual(len(candles), 1)
            self.assertEqual(candles[0].close, 2.5)

    def test_upsert_records_last_updated_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")

            store.upsert_many(
                "600519",
                [Candle(date=date(2026, 5, 20), open=10, high=12, low=9, close=11, volume=100, amount=1100)],
            )

            last_updated_at = store.get_last_updated_at()

            self.assertIsNotNone(last_updated_at)
            self.assertIn("T", str(last_updated_at))

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
            self.assertIsNotNone(target.get_last_updated_at())

    def test_replace_symbol_history_deletes_older_rows_not_present_in_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            store.upsert_many(
                "600519",
                [
                    Candle(date=date(2026, 5, 19), open=9, high=11, low=8, close=10, volume=90, amount=900),
                    Candle(date=date(2026, 5, 20), open=10, high=12, low=9, close=11, volume=100, amount=1100),
                ],
            )

            replaced = store.replace_symbol_history(
                "600519",
                [Candle(date=date(2026, 5, 20), open=10, high=12, low=9, close=11.2, volume=101, amount=1131)],
            )

            self.assertEqual(replaced, 1)
            candles = store.get_latest("600519", limit=10)
            self.assertEqual(len(candles), 1)
            self.assertEqual(candles[0].date, date(2026, 5, 20))
            self.assertEqual(candles[0].close, 11.2)

    def test_replace_symbol_history_from_date_preserves_older_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            store.upsert_many(
                "600519",
                [
                    Candle(date=date(2026, 5, 19), open=9, high=11, low=8, close=10, volume=90, amount=900),
                    Candle(date=date(2026, 5, 20), open=10, high=12, low=9, close=11, volume=100, amount=1100),
                    Candle(date=date(2026, 5, 21), open=11, high=13, low=10, close=12, volume=120, amount=1440),
                ],
            )

            replaced = store.replace_symbol_history_from_date(
                "600519",
                [
                    Candle(date=date(2026, 5, 20), open=10, high=12, low=9, close=11.2, volume=101, amount=1131),
                    Candle(date=date(2026, 5, 22), open=12, high=14, low=11, close=13, volume=130, amount=1690),
                ],
                date(2026, 5, 20),
            )

            self.assertEqual(replaced, 2)
            candles = store.get_latest("600519", limit=10)
            self.assertEqual([candle.date for candle in candles], [date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 22)])
            self.assertEqual(candles[0].close, 10)
            self.assertEqual(candles[1].close, 11.2)
            self.assertEqual(candles[2].close, 13)

    def test_get_earliest_date_returns_first_cached_trade_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            store.upsert_many(
                "600519",
                [
                    Candle(date=date(2026, 5, 20), open=10, high=12, low=9, close=11, volume=100, amount=1100),
                    Candle(date=date(2026, 5, 19), open=9, high=11, low=8, close=10, volume=90, amount=900),
                ],
            )

            earliest = store.get_earliest_date("600519")

            self.assertEqual(earliest, date(2026, 5, 19))


if __name__ == "__main__":
    unittest.main()

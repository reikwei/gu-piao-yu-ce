import tempfile
import unittest
from datetime import date
from pathlib import Path

from kronos_mvp.models import Candle
from kronos_mvp.providers import MemoryProvider, ProviderError
from kronos_mvp.storage import CandleStore
from kronos_mvp.sync import DataSyncService


class DataSyncServiceTests(unittest.TestCase):
    def test_sync_uses_next_provider_when_first_provider_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            failing = MemoryProvider("failing", error=ProviderError("offline"))
            working = MemoryProvider(
                "working",
                candles=[Candle(date=date(2026, 5, 21), open=1, high=2, low=1, close=1.8, volume=10, amount=18)],
            )
            service = DataSyncService(store=store, providers=[failing, working])

            result = service.sync_symbol("600519")

            self.assertEqual(result.provider, "working")
            self.assertEqual(result.rows, 1)
            self.assertEqual(store.get_latest("600519", limit=1)[0].close, 1.8)

    def test_sync_raises_clear_error_when_all_providers_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            service = DataSyncService(store=store, providers=[MemoryProvider("a", error=ProviderError("blocked"))])

            with self.assertRaisesRegex(ProviderError, "a: blocked"):
                service.sync_symbol("000001")

    def test_sync_only_fetches_rows_after_latest_cached_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            store.upsert_many(
                "600519",
                [Candle(date=date(2026, 5, 21), open=1, high=2, low=1, close=1.8, volume=10, amount=18)],
            )
            provider = MemoryProvider(
                "working",
                candles=[
                    Candle(date=date(2026, 5, 21), open=1, high=2, low=1, close=1.8, volume=10, amount=18),
                    Candle(date=date(2026, 5, 22), open=1.8, high=2.1, low=1.7, close=2.0, volume=12, amount=24),
                ],
            )
            service = DataSyncService(store=store, providers=[provider])

            result = service.sync_symbol("600519")

            self.assertEqual(result.rows, 1)
            self.assertEqual(store.get_latest_date("600519"), date(2026, 5, 22))
            self.assertEqual(store.get_latest("600519", limit=2)[-1].close, 2.0)

    def test_sync_returns_zero_when_incremental_run_has_no_new_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CandleStore(Path(tmp) / "candles.db")
            store.upsert_many(
                "600519",
                [Candle(date=date(2026, 5, 22), open=1, high=2, low=1, close=1.8, volume=10, amount=18)],
            )
            service = DataSyncService(
                store=store,
                providers=[
                    MemoryProvider(
                        "working",
                        candles=[Candle(date=date(2026, 5, 22), open=1, high=2, low=1, close=1.8, volume=10, amount=18)],
                    )
                ],
            )

            result = service.sync_symbol("600519")

            self.assertEqual(result.rows, 0)
            self.assertEqual(store.get_latest_date("600519"), date(2026, 5, 22))


if __name__ == "__main__":
    unittest.main()

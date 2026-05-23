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


if __name__ == "__main__":
    unittest.main()

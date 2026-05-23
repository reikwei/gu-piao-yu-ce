import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from kronos_mvp.cli import main
from kronos_mvp.models import SyncResult


class CliTests(unittest.TestCase):
    def test_sync_all_uses_market_symbol_list(self):
        sync_service = Mock()
        sync_service.sync_symbol.side_effect = [
            SyncResult(symbol="000001", provider="baostock", rows=1),
            SyncResult(symbol="600519", provider="baostock", rows=0),
        ]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "sys.argv", ["prog", "--db", str(Path(tmp) / "candles.db"), "sync", "--all"]
        ), patch("kronos_mvp.cli.CandleStore"), patch(
            "kronos_mvp.cli.build_default_providers", return_value=[]
        ), patch(
            "kronos_mvp.cli.DataSyncService", return_value=sync_service
        ), patch(
            "kronos_mvp.cli.list_a_share_symbols", return_value=["000001", "600519"]
        ), redirect_stdout(io.StringIO()) as stdout:
            main()

        self.assertEqual(sync_service.sync_symbol.call_count, 2)
        self.assertIn('"mode": "all"', stdout.getvalue())
        self.assertIn('"updated": 1', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
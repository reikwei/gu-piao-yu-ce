import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import Mock, call, patch

from kronos_mvp.cli import main
from kronos_mvp.funds import FundSyncWindowResult
from kronos_mvp.models import SyncResult


class CliTests(unittest.TestCase):
    def test_sync_funds_emits_summary(self):
        sync_service = Mock()
        sync_service.sync_recent.return_value = FundSyncWindowResult(
            target_date=date(2026, 5, 23),
            requested_days=15,
            synced_trade_dates=(date(2026, 5, 9), date(2026, 5, 23)),
            skipped_trade_dates=(date(2026, 5, 22),),
            rows=3800,
            providers=("akshare",),
        )

        with tempfile.TemporaryDirectory() as tmp, patch(
            "sys.argv", ["prog", "--fund-db", str(Path(tmp) / "fund_factors.db"), "sync-funds"]
        ), patch("kronos_mvp.cli.FundFactorStore"), patch(
            "kronos_mvp.cli.build_default_fund_providers", return_value=[]
        ), patch("kronos_mvp.cli.FundFactorSyncService", return_value=sync_service), redirect_stdout(io.StringIO()) as stdout:
            main()

        sync_service.sync_recent.assert_called_once_with(history_days=15)
        self.assertIn('"mode": "funds"', stdout.getvalue())
        self.assertIn('"rows": 3800', stdout.getvalue())
        self.assertIn('"requestedDays": 15', stdout.getvalue())

    def test_sync_relative_emits_summary(self):
        sync_service = Mock()
        sync_service.sync_market.return_value.to_dict.return_value = {
            "provider": "akshare",
            "targetSymbols": ["600835"],
            "benchmarkLabels": ["上证指数"],
            "industryNames": ["家电行业"],
            "mappingRows": 4120,
            "rows": 186,
            "warnings": [],
        }
        sync_service.sync_market.return_value.target_symbols = ("600835",)
        sync_service.sync_market.return_value.benchmark_labels = ("上证指数",)
        sync_service.sync_market.return_value.industry_names = ("家电行业",)
        sync_service.sync_market.return_value.mapping_rows = 4120
        sync_service.sync_market.return_value.rows = 186
        sync_service.sync_market.return_value.warnings = ()

        with tempfile.TemporaryDirectory() as tmp, patch(
            "sys.argv",
            [
                "prog",
                "--relative-db",
                str(Path(tmp) / "relative_strength.db"),
                "sync-relative",
                "600835",
                "--history-days",
                "45",
            ],
        ), patch("kronos_mvp.cli.RelativeStrengthStore"), patch(
            "kronos_mvp.cli.RelativeStrengthSyncService", return_value=sync_service
        ), redirect_stdout(io.StringIO()) as stdout:
            main()

        sync_service.sync_market.assert_called_once_with(history_days=45, symbols=["600835"])
        self.assertIn('"mode": "relative-strength"', stdout.getvalue())
        self.assertIn('"mappingRows": 4120', stdout.getvalue())
        self.assertIn('"industryCount": 1', stdout.getvalue())

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
        ) as list_symbols, redirect_stdout(io.StringIO()) as stdout:
            main()

        self.assertEqual(sync_service.sync_symbol.call_count, 2)
        list_symbols.assert_called_once_with(market="all")
        self.assertIn('"mode": "all"', stdout.getvalue())
        self.assertIn('"updated": 1', stdout.getvalue())

    def test_sync_all_filters_symbols_by_prefixes(self):
        sync_service = Mock()
        sync_service.sync_symbol.side_effect = [
            SyncResult(symbol="600519", provider="baostock", rows=1),
            SyncResult(symbol="688001", provider="baostock", rows=0),
        ]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "sys.argv",
            [
                "prog",
                "--db",
                str(Path(tmp) / "candles.db"),
                "sync",
                "--all",
                "--market",
                "sh",
                "--prefixes",
                "600,688",
            ],
        ), patch("kronos_mvp.cli.CandleStore"), patch(
            "kronos_mvp.cli.build_default_providers", return_value=[]
        ), patch(
            "kronos_mvp.cli.DataSyncService", return_value=sync_service
        ), patch(
            "kronos_mvp.cli.list_a_share_symbols", return_value=["600519", "603288", "688001"]
        ) as list_symbols, redirect_stdout(io.StringIO()) as stdout:
            main()

        self.assertEqual(sync_service.sync_symbol.call_args_list, [call("600519"), call("688001")])
        list_symbols.assert_called_once_with(market="sh")
        self.assertIn('"prefixes": ["600", "688"]', stdout.getvalue())

    def test_sync_all_retries_failed_symbol_and_removes_progress_file_after_success(self):
        sync_service = Mock()
        attempts = {"000001": 0}

        def sync_symbol(symbol: str) -> SyncResult:
            if symbol == "000001":
                attempts[symbol] += 1
                if attempts[symbol] == 1:
                    raise RuntimeError("offline")
                return SyncResult(symbol="000001", provider="baostock", rows=1)
            return SyncResult(symbol=symbol, provider="baostock", rows=0)

        sync_service.sync_symbol.side_effect = sync_symbol

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "candles.db"
            progress_path = Path(tmp) / "progress.json"
            with patch(
                "sys.argv",
                [
                    "prog",
                    "--db",
                    str(db_path),
                    "sync",
                    "--all",
                    "--progress-file",
                    str(progress_path),
                    "--max-retries",
                    "1",
                ],
            ), patch("kronos_mvp.cli.build_default_providers", return_value=[]), patch(
                "kronos_mvp.cli.DataSyncService", return_value=sync_service
            ), patch(
                "kronos_mvp.cli.list_a_share_symbols", return_value=["000001", "600519"]
            ), redirect_stdout(io.StringIO()) as stdout:
                main()

        self.assertEqual(
            sync_service.sync_symbol.call_args_list,
            [call("000001"), call("600519"), call("000001")],
        )
        self.assertFalse(progress_path.exists())
        self.assertIn('"retrying": true', stdout.getvalue())
        self.assertIn('"retried": 1', stdout.getvalue())

    def test_sync_all_resumes_from_saved_progress_without_refetching_symbol_list(self):
        sync_service = Mock()
        sync_service.sync_symbol.return_value = SyncResult(symbol="000001", provider="baostock", rows=0)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "candles.db"
            progress_path = Path(tmp) / "progress.json"
            progress_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mode": "all",
                        "market": "sz",
                        "pending": ["000001"],
                        "attempts": {},
                        "failed_symbols": [],
                        "summary": {
                            "symbols": 2,
                            "processed": 1,
                            "succeeded": 1,
                            "failed": 0,
                            "updated": 1,
                            "rows": 2,
                            "retried": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch(
                "sys.argv",
                [
                    "prog",
                    "--db",
                    str(db_path),
                    "sync",
                    "--all",
                    "--market",
                    "sz",
                    "--progress-file",
                    str(progress_path),
                ],
            ), patch("kronos_mvp.cli.build_default_providers", return_value=[]), patch(
                "kronos_mvp.cli.DataSyncService", return_value=sync_service
            ), patch("kronos_mvp.cli.list_a_share_symbols") as list_symbols, redirect_stdout(io.StringIO()) as stdout:
                main()

        list_symbols.assert_not_called()
        sync_service.sync_symbol.assert_called_once_with("000001")
        self.assertFalse(progress_path.exists())
        self.assertIn('"resume": true', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
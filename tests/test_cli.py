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

        sync_service.sync_market.assert_called_once_with(
            history_days=45,
            symbols=["600835"],
            force_refresh_mappings=False,
        )
        self.assertIn('"mode": "relative-strength"', stdout.getvalue())
        self.assertIn('"mappingRows": 4120', stdout.getvalue())
        self.assertIn('"industryCount": 1', stdout.getvalue())

    def test_sync_relative_refresh_mappings_flag_is_forwarded(self):
        sync_service = Mock()
        sync_service.sync_market.return_value.to_dict.return_value = {
            "provider": "akshare",
            "targetSymbols": [],
            "benchmarkLabels": ["沪深300", "上证指数", "深证成指", "创业板指"],
            "industryNames": ["家电行业"],
            "mappingRows": 4120,
            "rows": 186,
            "warnings": [],
        }
        sync_service.sync_market.return_value.target_symbols = ()
        sync_service.sync_market.return_value.benchmark_labels = ("沪深300", "上证指数", "深证成指", "创业板指")
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
                "--refresh-mappings",
            ],
        ), patch("kronos_mvp.cli.RelativeStrengthStore"), patch(
            "kronos_mvp.cli.RelativeStrengthSyncService", return_value=sync_service
        ), redirect_stdout(io.StringIO()) as stdout:
            main()

        sync_service.sync_market.assert_called_once_with(history_days=30, force_refresh_mappings=True)
        self.assertIn('"forceRefreshMappings": true', stdout.getvalue())

    def test_sync_news_emits_summary(self):
        sync_service = Mock()
        sync_service.sync_symbol.side_effect = [
            Mock(to_dict=lambda: {"symbol": "600519", "provider": "akshare", "rows": 12}, rows=12),
            Mock(to_dict=lambda: {"symbol": "000001", "provider": "akshare", "rows": 7}, rows=7),
        ]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "sys.argv",
            [
                "prog",
                "--news-db",
                str(Path(tmp) / "news_sentiment.db"),
                "sync-news",
                "600519",
                "000001",
                "--limit",
                "25",
            ],
        ), patch("kronos_mvp.cli.StockNewsStore"), patch(
            "kronos_mvp.cli.build_default_news_providers", return_value=[]
        ), patch(
            "kronos_mvp.cli.StockNewsSyncService", return_value=sync_service
        ), redirect_stdout(io.StringIO()) as stdout:
            main()

        self.assertEqual(sync_service.sync_symbol.call_args_list, [call("600519", limit=25), call("000001", limit=25)])
        self.assertIn('"mode": "news"', stdout.getvalue())
        self.assertIn('"rows": 19', stdout.getvalue())

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

    def test_sync_all_full_refresh_passes_flag_to_service(self):
        sync_service = Mock()
        sync_service.sync_symbol.side_effect = [
            SyncResult(symbol="000001", provider="baostock", rows=1),
            SyncResult(symbol="600519", provider="baostock", rows=1),
        ]

        with tempfile.TemporaryDirectory() as tmp, patch(
            "sys.argv", ["prog", "--db", str(Path(tmp) / "candles.db"), "sync", "--all", "--full-refresh"]
        ), patch("kronos_mvp.cli.CandleStore"), patch(
            "kronos_mvp.cli.build_default_providers", return_value=[]
        ), patch(
            "kronos_mvp.cli.DataSyncService", return_value=sync_service
        ), patch(
            "kronos_mvp.cli.list_a_share_symbols", return_value=["000001", "600519"]
        ), redirect_stdout(io.StringIO()) as stdout:
            main()

        self.assertEqual(
            sync_service.sync_symbol.call_args_list,
            [call("000001", full_refresh=True), call("600519", full_refresh=True)],
        )
        self.assertIn('"fullRefresh": true', stdout.getvalue())

    def test_sync_symbol_full_refresh_passes_flag_to_service(self):
        sync_service = Mock()
        sync_service.sync_symbol.return_value = SyncResult(symbol="600519", provider="baostock", rows=3)

        with tempfile.TemporaryDirectory() as tmp, patch(
            "sys.argv", ["prog", "--db", str(Path(tmp) / "candles.db"), "sync", "600519", "--full-refresh"]
        ), patch("kronos_mvp.cli.CandleStore"), patch(
            "kronos_mvp.cli.build_default_providers", return_value=[]
        ), patch(
            "kronos_mvp.cli.DataSyncService", return_value=sync_service
        ), redirect_stdout(io.StringIO()) as stdout:
            main()

        sync_service.sync_symbol.assert_called_once_with("600519", full_refresh=True)
        self.assertIn('"fullRefresh": true', stdout.getvalue())

    def test_serve_uses_api_factory(self):
        fake_uvicorn = Mock()

        with patch("sys.argv", ["prog", "serve", "--host", "0.0.0.0", "--port", "9000"]), patch.dict(
            "sys.modules", {"uvicorn": fake_uvicorn}
        ):
            main()

        fake_uvicorn.run.assert_called_once_with(
            "kronos_mvp.api:create_app",
            host="0.0.0.0",
            port=9000,
            reload=False,
            factory=True,
        )

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

        self.assertEqual(
            sync_service.sync_symbol.call_args_list,
            [call("600519", full_refresh=False), call("688001", full_refresh=False)],
        )
        list_symbols.assert_called_once_with(market="sh")
        self.assertIn('"prefixes": ["600", "688"]', stdout.getvalue())

    def test_sync_all_retries_failed_symbol_and_removes_progress_file_after_success(self):
        sync_service = Mock()
        attempts = {"000001": 0}

        def sync_symbol(symbol: str, full_refresh: bool = False) -> SyncResult:
            self.assertFalse(full_refresh)
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
            [
                call("000001", full_refresh=False),
                call("600519", full_refresh=False),
                call("000001", full_refresh=False),
            ],
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
        sync_service.sync_symbol.assert_called_once_with("000001", full_refresh=False)
        self.assertFalse(progress_path.exists())
        self.assertIn('"resume": true', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
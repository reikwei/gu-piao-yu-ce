import tempfile
import unittest
from datetime import date
from pathlib import Path

from kronos_mvp.funds import (
    FundFactor,
    FundFactorStore,
    FundFactorSyncService,
    FundSyncWindowResult,
    MemoryFundFactorProvider,
    _normalize_provider_symbol,
    build_fund_analysis,
)
from kronos_mvp.providers import ProviderError


class FundFactorStoreTests(unittest.TestCase):
    def test_upsert_and_read_latest_factors_in_date_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FundFactorStore(Path(tmp) / "fund_factors.db")
            store.upsert_many(
                [
                    FundFactor(symbol="600519", trade_date=date(2026, 5, 20), fund_net_inflow=100, fund_net_inflow_ratio=1.2),
                    FundFactor(symbol="600519", trade_date=date(2026, 5, 21), fund_net_inflow=120, fund_net_inflow_ratio=1.4),
                    FundFactor(symbol="600519", trade_date=date(2026, 5, 19), fund_net_inflow=90, fund_net_inflow_ratio=1.0),
                ]
            )

            factors = store.get_latest("600519", limit=2)

            self.assertEqual([factor.trade_date for factor in factors], [date(2026, 5, 20), date(2026, 5, 21)])
            self.assertEqual(factors[-1].fund_net_inflow, 120)

    def test_merge_from_imports_rows_from_other_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = FundFactorStore(Path(tmp) / "target.db")
            source = FundFactorStore(Path(tmp) / "source.db")
            source.upsert_many(
                [FundFactor(symbol="600519", trade_date=date(2026, 5, 23), fund_net_inflow=188, fund_net_inflow_ratio=2.4)]
            )

            merged = target.merge_from(source.db_path)

            self.assertEqual(merged, 1)
            factors = target.get_latest("600519", limit=1)
            self.assertEqual(len(factors), 1)
            self.assertEqual(factors[0].trade_date, date(2026, 5, 23))


class FundFactorSyncServiceTests(unittest.TestCase):
    def test_sync_latest_uses_next_provider_when_first_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FundFactorStore(Path(tmp) / "fund_factors.db")
            failing = MemoryFundFactorProvider("failing", error=ProviderError("offline"))
            working = MemoryFundFactorProvider(
                "working",
                factors=[
                    FundFactor(
                        symbol="600519",
                        trade_date=date(2026, 5, 23),
                        fund_net_inflow=1.8e8,
                        fund_net_inflow_ratio=3.1,
                        margin_balance=9.0e9,
                        margin_buy_amount=4.2e8,
                    )
                ],
            )
            service = FundFactorSyncService(store=store, providers=[failing, working])

            result = service.sync_latest(trade_date=date(2026, 5, 23))

            self.assertEqual(result.provider, "working")
            self.assertEqual(result.rows, 1)
            self.assertEqual(store.get_latest("600519", limit=1)[0].fund_net_inflow_ratio, 3.1)

    def test_sync_latest_raises_clear_error_when_all_providers_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FundFactorStore(Path(tmp) / "fund_factors.db")
            service = FundFactorSyncService(store=store, providers=[MemoryFundFactorProvider("a", error=ProviderError("blocked"))])

            with self.assertRaisesRegex(ProviderError, "a: blocked"):
                service.sync_latest(trade_date=date(2026, 5, 23))

    def test_sync_recent_backfills_missing_trade_dates_and_refreshes_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FundFactorStore(Path(tmp) / "fund_factors.db")
            store.upsert_many([FundFactor(symbol="600519", trade_date=date(2026, 5, 21), fund_net_inflow=100)])
            store.upsert_many([FundFactor(symbol="600519", trade_date=date(2026, 5, 23), fund_net_inflow=160)])
            service = FundFactorSyncService(
                store=store,
                providers=[
                    MemoryFundFactorProvider(
                        "working",
                        factors=[
                            FundFactor(symbol="600519", trade_date=date(2026, 5, 21), fund_net_inflow=100),
                            FundFactor(symbol="600519", trade_date=date(2026, 5, 22), fund_net_inflow=120),
                            FundFactor(symbol="600519", trade_date=date(2026, 5, 23), fund_net_inflow=180),
                        ],
                    )
                ],
            )

            with unittest.mock.patch(
                "kronos_mvp.funds.recent_a_share_trade_dates",
                return_value=[date(2026, 5, 21), date(2026, 5, 22), date(2026, 5, 23)],
            ):
                result = service.sync_recent(history_days=3, trade_date=date(2026, 5, 23))

            self.assertIsInstance(result, FundSyncWindowResult)
            self.assertEqual(result.synced_trade_dates, (date(2026, 5, 22), date(2026, 5, 23)))
            self.assertEqual(result.skipped_trade_dates, (date(2026, 5, 21),))
            self.assertEqual(store.get_latest("600519", limit=2)[-1].fund_net_inflow, 180)


class FundAnalysisTests(unittest.TestCase):
    def test_normalize_provider_symbol_restores_leading_zero_codes(self):
        self.assertEqual(_normalize_provider_symbol(1), "000001")
        self.assertEqual(_normalize_provider_symbol("333"), "000333")
        self.assertEqual(_normalize_provider_symbol(1237.0), "001237")
        self.assertEqual(_normalize_provider_symbol("600519"), "600519")

    def test_build_fund_analysis_returns_bullish_signal(self):
        analysis = build_fund_analysis(
            [
                FundFactor(symbol="600519", trade_date=date(2026, 5, 12), fund_net_inflow=5.0e7, fund_net_inflow_ratio=1.2, margin_balance=8.10e9, margin_buy_amount=2.4e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 13), fund_net_inflow=4.5e7, fund_net_inflow_ratio=1.1, margin_balance=8.18e9, margin_buy_amount=2.5e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 14), fund_net_inflow=6.2e7, fund_net_inflow_ratio=1.8, margin_balance=8.25e9, margin_buy_amount=2.7e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 15), fund_net_inflow=7.0e7, fund_net_inflow_ratio=2.0, margin_balance=8.36e9, margin_buy_amount=2.9e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 16), fund_net_inflow=7.5e7, fund_net_inflow_ratio=2.1, margin_balance=8.48e9, margin_buy_amount=3.0e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 19), fund_net_inflow=8.0e7, fund_net_inflow_ratio=2.4, margin_balance=8.60e9, margin_buy_amount=3.1e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 20), fund_net_inflow=9.5e7, fund_net_inflow_ratio=2.7, margin_balance=8.76e9, margin_buy_amount=3.3e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 21), fund_net_inflow=1.0e8, fund_net_inflow_ratio=2.9, margin_balance=8.84e9, margin_buy_amount=3.5e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 22), fund_net_inflow=1.2e8, fund_net_inflow_ratio=3.1, margin_balance=8.92e9, margin_buy_amount=3.8e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 23), fund_net_inflow=1.5e8, fund_net_inflow_ratio=3.6, margin_balance=9.00e9, margin_buy_amount=4.2e8),
            ]
        )

        self.assertEqual(analysis["signal"], "bullish")
        self.assertEqual(analysis["signalLabel"], "偏多")
        self.assertGreaterEqual(analysis["score"], 70)
        self.assertEqual(analysis["trendProfile"]["label"], "持续趋势")
        self.assertEqual(analysis["flowMetrics"]["consecutiveInflowDays"], 10)
        self.assertAlmostEqual(analysis["flowMetrics"]["netInflow3d"], 370000000.0)
        self.assertAlmostEqual(analysis["flowMetrics"]["netInflow5d"], 545000000.0)
        self.assertAlmostEqual(analysis["flowMetrics"]["netInflow10d"], 847000000.0)
        self.assertIsNotNone(analysis["marginMetrics"]["balanceSlope3d"])

    def test_build_fund_analysis_distinguishes_single_day_anomaly(self):
        analysis = build_fund_analysis(
            [
                FundFactor(symbol="600519", trade_date=date(2026, 5, 19), fund_net_inflow=-7.0e7, fund_net_inflow_ratio=-1.4, margin_balance=8.80e9, margin_buy_amount=3.1e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 20), fund_net_inflow=-6.0e7, fund_net_inflow_ratio=-1.1, margin_balance=8.72e9, margin_buy_amount=2.9e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 21), fund_net_inflow=-5.0e7, fund_net_inflow_ratio=-0.8, margin_balance=8.68e9, margin_buy_amount=2.8e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 22), fund_net_inflow=-4.0e7, fund_net_inflow_ratio=-0.6, margin_balance=8.65e9, margin_buy_amount=2.7e8),
                FundFactor(symbol="600519", trade_date=date(2026, 5, 23), fund_net_inflow=1.8e8, fund_net_inflow_ratio=3.2, margin_balance=8.66e9, margin_buy_amount=3.4e8),
            ]
        )

        self.assertEqual(analysis["trendProfile"]["label"], "单日异动")
        self.assertEqual(analysis["flowMetrics"]["consecutiveInflowDays"], 1)
        self.assertIn("单日异动", analysis["summary"])


if __name__ == "__main__":
    unittest.main()
import tempfile
import unittest
from datetime import date
from pathlib import Path

from kronos_mvp.funds import (
    FundFactor,
    FundFactorStore,
    FundFactorSyncService,
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


class FundAnalysisTests(unittest.TestCase):
    def test_normalize_provider_symbol_restores_leading_zero_codes(self):
        self.assertEqual(_normalize_provider_symbol(1), "000001")
        self.assertEqual(_normalize_provider_symbol("333"), "000333")
        self.assertEqual(_normalize_provider_symbol(1237.0), "001237")
        self.assertEqual(_normalize_provider_symbol("600519"), "600519")

    def test_build_fund_analysis_returns_bullish_signal(self):
        analysis = build_fund_analysis(
            [
                FundFactor(
                    symbol="600519",
                    trade_date=date(2026, 5, 22),
                    fund_net_inflow=8.0e7,
                    fund_net_inflow_ratio=1.6,
                    margin_balance=8.5e9,
                    margin_buy_amount=3.1e8,
                ),
                FundFactor(
                    symbol="600519",
                    trade_date=date(2026, 5, 23),
                    fund_net_inflow=1.5e8,
                    fund_net_inflow_ratio=3.6,
                    margin_balance=9.0e9,
                    margin_buy_amount=4.2e8,
                ),
            ]
        )

        self.assertEqual(analysis["signal"], "bullish")
        self.assertEqual(analysis["signalLabel"], "偏多")
        self.assertGreaterEqual(analysis["score"], 70)


if __name__ == "__main__":
    unittest.main()
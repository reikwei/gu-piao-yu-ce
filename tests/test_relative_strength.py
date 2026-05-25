import tempfile
import unittest
from datetime import datetime
from datetime import date
from pathlib import Path

from kronos_mvp.models import Candle
from kronos_mvp.providers import ProviderError
from kronos_mvp.relative_strength import (
    RelativeStrengthStore,
    RelativeStrengthSyncService,
    SymbolIndustry,
    build_relative_strength_analysis,
    industry_key_from_name,
)
from kronos_mvp.storage import SHANGHAI_TZ


class _Mapping:
    def __init__(self, symbol: str, industry_name: str):
        self.symbol = symbol
        self.industry_name = industry_name


class MemoryRelativeStrengthProvider:
    name = "memory"

    def __init__(
        self,
        mapping_error: Exception | None = None,
        index_error: Exception | None = None,
        industry_error: Exception | None = None,
    ):
        self.mapping_calls = 0
        self.index_calls: list[tuple[str, date | None]] = []
        self.industry_calls: list[tuple[str, date | None]] = []
        self.mapping_error = mapping_error
        self.index_error = index_error
        self.industry_error = industry_error

    def fetch_index_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        self.index_calls.append((symbol, start_date))
        if self.index_error is not None:
            raise self.index_error
        return [
            Candle(date=date(2026, 5, 22), open=3200, high=3230, low=3190, close=3220, volume=1_000_000, amount=2_000_000),
            Candle(date=date(2026, 5, 23), open=3210, high=3240, low=3200, close=3235, volume=1_100_000, amount=2_100_000),
        ]

    def fetch_industry_mappings(self):
        self.mapping_calls += 1
        if self.mapping_error is not None:
            raise self.mapping_error
        return [_Mapping(symbol="600835", industry_name="家电行业")]

    def fetch_industry_daily(self, industry_name: str, start_date: date | None = None) -> list[Candle]:
        self.industry_calls.append((industry_name, start_date))
        if self.industry_error is not None:
            raise self.industry_error
        return [
            Candle(date=date(2026, 5, 22), open=1500, high=1510, low=1490, close=1502, volume=500_000, amount=900_000),
            Candle(date=date(2026, 5, 23), open=1504, high=1518, low=1500, close=1512, volume=520_000, amount=920_000),
        ]


def _sample_stock_candles() -> list[Candle]:
    closes = [10.00, 10.18, 10.32, 10.50, 10.70, 10.92, 11.08, 11.26, 11.48, 11.72, 11.96, 12.22, 12.50, 12.78, 13.05, 13.36, 13.72, 14.08, 14.46, 14.88, 15.30]
    candles: list[Candle] = []
    for day, close in enumerate(closes, start=1):
        candles.append(
            Candle(
                date=date(2026, 5, day),
                open=round(close * 0.99, 2),
                high=round(close * 1.02, 2),
                low=round(close * 0.98, 2),
                close=close,
                volume=1000 + day * 10,
                amount=close * (1000 + day * 10),
            )
        )
    return candles


def _sample_reference_candles(start_close: float, daily_step: float) -> list[Candle]:
    candles: list[Candle] = []
    close = start_close
    for day in range(1, 22):
        candles.append(
            Candle(
                date=date(2026, 5, day),
                open=round(close * 0.995, 2),
                high=round(close * 1.01, 2),
                low=round(close * 0.99, 2),
                close=round(close, 2),
                volume=800 + day * 8,
                amount=close * (800 + day * 8),
            )
        )
        close += daily_step
    return candles


class RelativeStrengthStoreTests(unittest.TestCase):
    def test_upsert_and_read_symbol_industry_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RelativeStrengthStore(Path(tmp) / "relative.db")
            store.upsert_symbol_industries(
                [
                    SymbolIndustry(
                        symbol="600835",
                        industry_key=industry_key_from_name("家电行业"),
                        industry_name="家电行业",
                        source="akshare",
                        updated_at="2026-05-25T16:30:00+08:00",
                    )
                ]
            )

            mapping = store.get_symbol_industry("600835")

        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.industry_name, "家电行业")
        self.assertEqual(mapping.industry_key, "industry:家电行业")


class RelativeStrengthSyncServiceTests(unittest.TestCase):
    def test_sync_symbol_writes_benchmark_and_industry_candles(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RelativeStrengthStore(Path(tmp) / "relative.db")
            provider = MemoryRelativeStrengthProvider()
            service = RelativeStrengthSyncService(store=store, provider=provider)

            result = service.sync_symbol("600835", history_days=20)

            self.assertEqual(result.provider, "memory")
            self.assertEqual(result.mapping_rows, 1)
            self.assertEqual(result.rows, 4)
            self.assertEqual(store.get_symbol_industry("600835").industry_name, "家电行业")
            self.assertEqual(store.get_latest("benchmark:sh", limit=1)[0].close, 3235)
            self.assertEqual(store.get_latest("industry:家电行业", limit=1)[0].close, 1512)

    def test_sync_market_reuses_fresh_mappings(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RelativeStrengthStore(Path(tmp) / "relative.db")
            provider = MemoryRelativeStrengthProvider()
            service = RelativeStrengthSyncService(store=store, provider=provider)
            store.upsert_symbol_industries(
                [
                    SymbolIndustry(
                        symbol="600835",
                        industry_key=industry_key_from_name("家电行业"),
                        industry_name="家电行业",
                        source="akshare",
                        updated_at=datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
                    )
                ]
            )

            result = service.sync_market(history_days=20)

            self.assertEqual(result.mapping_rows, 0)
            self.assertEqual(provider.mapping_calls, 0)
            self.assertEqual(result.benchmark_labels, ("沪深300", "上证指数", "深证成指", "创业板指"))
            self.assertEqual(result.industry_names, ("家电行业",))

    def test_sync_market_reuses_cached_mappings_when_refresh_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RelativeStrengthStore(Path(tmp) / "relative.db")
            provider = MemoryRelativeStrengthProvider(mapping_error=ProviderError("upstream closed connection"))
            service = RelativeStrengthSyncService(store=store, provider=provider)
            store.upsert_symbol_industries(
                [
                    SymbolIndustry(
                        symbol="600835",
                        industry_key=industry_key_from_name("家电行业"),
                        industry_name="家电行业",
                        source="akshare",
                        updated_at="2026-05-01T16:30:00+08:00",
                    )
                ]
            )

            result = service.sync_market(history_days=20)

            self.assertEqual(result.mapping_rows, 0)
            self.assertEqual(provider.mapping_calls, 1)
            self.assertEqual(result.industry_names, ("家电行业",))
            self.assertTrue(result.warnings)
            self.assertIn("行业映射刷新失败", result.warnings[0])

    def test_sync_market_falls_back_to_benchmarks_when_mapping_refresh_fails_without_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RelativeStrengthStore(Path(tmp) / "relative.db")
            provider = MemoryRelativeStrengthProvider(mapping_error=ProviderError("upstream closed connection"))
            service = RelativeStrengthSyncService(store=store, provider=provider)

            result = service.sync_market(history_days=20)

            self.assertEqual(result.mapping_rows, 0)
            self.assertEqual(provider.mapping_calls, 1)
            self.assertEqual(result.benchmark_labels, ("沪深300", "上证指数", "深证成指", "创业板指"))
            self.assertEqual(result.industry_names, ())
            self.assertEqual(result.rows, 8)
            self.assertTrue(result.warnings)
            self.assertIn("本次仅同步指数基准", result.warnings[0])

    def test_sync_market_skips_failed_benchmarks_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RelativeStrengthStore(Path(tmp) / "relative.db")
            provider = MemoryRelativeStrengthProvider(index_error=ProviderError("upstream closed connection"))
            service = RelativeStrengthSyncService(store=store, provider=provider)
            store.upsert_symbol_industries(
                [
                    SymbolIndustry(
                        symbol="600835",
                        industry_key=industry_key_from_name("家电行业"),
                        industry_name="家电行业",
                        source="akshare",
                        updated_at=datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
                    )
                ]
            )

            result = service.sync_market(history_days=20)

            self.assertEqual(result.rows, 2)
            self.assertEqual(result.industry_names, ("家电行业",))
            self.assertEqual(result.benchmark_labels, ("沪深300", "上证指数", "深证成指", "创业板指"))
            self.assertTrue(any("指数同步失败" in warning for warning in result.warnings))


class RelativeStrengthAnalysisTests(unittest.TestCase):
    def test_build_relative_strength_analysis_flags_outperformance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RelativeStrengthStore(Path(tmp) / "relative.db")
            store.upsert_symbol_industries(
                [
                    SymbolIndustry(
                        symbol="600835",
                        industry_key=industry_key_from_name("家电行业"),
                        industry_name="家电行业",
                        source="akshare",
                        updated_at="2026-05-25T16:30:00+08:00",
                    )
                ]
            )
            store.upsert_candles("benchmark:sh", _sample_reference_candles(10.0, 0.08))
            store.upsert_candles("industry:家电行业", _sample_reference_candles(10.0, 0.11))

            analysis = build_relative_strength_analysis("600835", _sample_stock_candles(), store)

        self.assertTrue(analysis["available"])
        self.assertEqual(analysis["signal"], "bullish")
        self.assertGreaterEqual(int(analysis["score"]), 65)
        self.assertIn("相对强弱：相对偏强", str(analysis["detail"]))
        self.assertIn("近 5 日相对所属行业超额", str(analysis["detail"]))
        self.assertEqual(analysis["metrics"]["coverage"], "full")

    def test_build_relative_strength_analysis_marks_benchmark_only_scope_as_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RelativeStrengthStore(Path(tmp) / "relative.db")
            store.upsert_symbol_industries(
                [
                    SymbolIndustry(
                        symbol="600835",
                        industry_key=industry_key_from_name("家电行业"),
                        industry_name="家电行业",
                        source="akshare",
                        updated_at="2026-05-25T16:30:00+08:00",
                    )
                ]
            )
            store.upsert_candles("benchmark:sh", _sample_reference_candles(10.0, 0.08))

            analysis = build_relative_strength_analysis("600835", _sample_stock_candles(), store)

        self.assertTrue(analysis["available"])
        self.assertEqual(analysis["signal"], "bullish")
        self.assertEqual(analysis["metrics"]["coverage"], "benchmark-only")
        self.assertFalse(bool(analysis["metrics"]["industryAvailable"]))
        self.assertLess(int(analysis["score"]), int(analysis["rawScore"]))
        self.assertIn("行业侧待补齐", str(analysis["detail"]))


if __name__ == "__main__":
    unittest.main()
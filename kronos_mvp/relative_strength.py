from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Candle
from .providers import AkShareRelativeStrengthProvider, ProviderError, RelativeStrengthProvider
from .storage import CandleStore, SHANGHAI_TZ, normalize_symbol


DEFAULT_RELATIVE_HISTORY_DAYS = 30
_MAPPING_STALE_DAYS = 5


@dataclass(frozen=True)
class RelativeBenchmark:
    key: str
    symbol: str
    label: str


@dataclass(frozen=True)
class SymbolIndustry:
    symbol: str
    industry_key: str
    industry_name: str
    updated_at: str
    source: str = "akshare"


@dataclass(frozen=True)
class RelativeStrengthSyncResult:
    provider: str
    target_symbols: tuple[str, ...]
    benchmark_labels: tuple[str, ...]
    industry_names: tuple[str, ...]
    mapping_rows: int
    rows: int
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "targetSymbols": list(self.target_symbols),
            "benchmarkLabels": list(self.benchmark_labels),
            "industryNames": list(self.industry_names),
            "mappingRows": self.mapping_rows,
            "rows": self.rows,
            "warnings": list(self.warnings),
        }


_BENCHMARKS = {
    "hs300": RelativeBenchmark(key="benchmark:hs300", symbol="sh000300", label="沪深300"),
    "sh": RelativeBenchmark(key="benchmark:sh", symbol="sh000001", label="上证指数"),
    "sz": RelativeBenchmark(key="benchmark:sz", symbol="sz399001", label="深证成指"),
    "cyb": RelativeBenchmark(key="benchmark:cyb", symbol="sz399006", label="创业板指"),
}


def benchmark_for_symbol(symbol: str) -> RelativeBenchmark:
    code = normalize_symbol(symbol)
    if code.startswith(("300", "301")):
        return _BENCHMARKS["cyb"]
    if code.startswith(("000", "001", "002", "003")):
        return _BENCHMARKS["sz"]
    if code.startswith(("43", "8", "92")):
        return _BENCHMARKS["hs300"]
    return _BENCHMARKS["sh"]


def available_benchmarks(symbols: list[str] | None = None) -> tuple[RelativeBenchmark, ...]:
    if not symbols:
        return (_BENCHMARKS["hs300"], _BENCHMARKS["sh"], _BENCHMARKS["sz"], _BENCHMARKS["cyb"])

    seen: set[str] = set()
    benchmarks: list[RelativeBenchmark] = []
    for symbol in symbols:
        benchmark = benchmark_for_symbol(symbol)
        if benchmark.key in seen:
            continue
        seen.add(benchmark.key)
        benchmarks.append(benchmark)
    return tuple(benchmarks)


def industry_key_from_name(industry_name: str) -> str:
    return f"industry:{industry_name.strip()}"


class RelativeStrengthStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.candle_store = CandleStore(self.db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS symbol_industries (
                    symbol TEXT PRIMARY KEY,
                    industry_key TEXT NOT NULL,
                    industry_name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_symbol_industries_industry_key ON symbol_industries(industry_key)"
            )

    def upsert_symbol_industries(self, mappings: list[SymbolIndustry]) -> int:
        if not mappings:
            return 0

        rows = [
            (
                normalize_symbol(mapping.symbol),
                mapping.industry_key,
                mapping.industry_name,
                mapping.source,
                mapping.updated_at,
            )
            for mapping in mappings
        ]
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO symbol_industries(symbol, industry_key, industry_name, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    industry_key = excluded.industry_key,
                    industry_name = excluded.industry_name,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get_symbol_industry(self, symbol: str) -> SymbolIndustry | None:
        normalized = normalize_symbol(symbol)
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT symbol, industry_key, industry_name, source, updated_at
                FROM symbol_industries
                WHERE symbol = ?
                """,
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return SymbolIndustry(
            symbol=str(row["symbol"]),
            industry_key=str(row["industry_key"]),
            industry_name=str(row["industry_name"]),
            source=str(row["source"]),
            updated_at=str(row["updated_at"]),
        )

    def list_symbol_industries(self, symbols: list[str] | None = None) -> list[SymbolIndustry]:
        normalized_symbols = [normalize_symbol(symbol) for symbol in (symbols or [])]
        with self._connection() as conn:
            if normalized_symbols:
                placeholders = ", ".join("?" for _ in normalized_symbols)
                rows = conn.execute(
                    f"""
                    SELECT symbol, industry_key, industry_name, source, updated_at
                    FROM symbol_industries
                    WHERE symbol IN ({placeholders})
                    ORDER BY symbol
                    """,
                    normalized_symbols,
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT symbol, industry_key, industry_name, source, updated_at
                    FROM symbol_industries
                    ORDER BY industry_key, symbol
                    """
                ).fetchall()
        return [
            SymbolIndustry(
                symbol=str(row["symbol"]),
                industry_key=str(row["industry_key"]),
                industry_name=str(row["industry_name"]),
                source=str(row["source"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def mapping_last_updated_at(self) -> str | None:
        with self._connection() as conn:
            row = conn.execute("SELECT MAX(updated_at) AS updated_at FROM symbol_industries").fetchone()
        if row is None or row["updated_at"] is None:
            return None
        return str(row["updated_at"])

    def mapping_is_stale(self, max_age_days: int = _MAPPING_STALE_DAYS) -> bool:
        value = self.mapping_last_updated_at()
        if not value:
            return True
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return True
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=SHANGHAI_TZ)
        return datetime.now(SHANGHAI_TZ) - moment > timedelta(days=max_age_days)

    def upsert_candles(self, symbol: str, candles: list[Candle]) -> int:
        return self.candle_store.upsert_many(symbol, candles)

    def get_latest(self, symbol: str, limit: int = 512) -> list[Candle]:
        return self.candle_store.get_latest(symbol, limit=limit)

    def get_latest_date(self, symbol: str) -> date | None:
        return self.candle_store.get_latest_date(symbol)

    def get_last_updated_at(self) -> str | None:
        return _latest_timestamp(self.candle_store.get_last_updated_at(), self.mapping_last_updated_at())


class RelativeStrengthSyncService:
    def __init__(self, store: RelativeStrengthStore, provider: RelativeStrengthProvider | None = None):
        self.store = store
        self.provider = provider or AkShareRelativeStrengthProvider()

    def sync_symbol(self, symbol: str, history_days: int = DEFAULT_RELATIVE_HISTORY_DAYS) -> RelativeStrengthSyncResult:
        normalized = normalize_symbol(symbol)
        warnings: list[str] = []
        if self.store.mapping_is_stale() or self.store.get_symbol_industry(normalized) is None:
            mapping_rows, mapping_warning = self._refresh_mappings_or_reuse_cache([normalized])
            if mapping_warning:
                warnings.append(mapping_warning)
        else:
            mapping_rows = 0

        benchmark = benchmark_for_symbol(normalized)
        rows = self._sync_benchmark(benchmark, history_days=history_days)
        mapping = self.store.get_symbol_industry(normalized)
        industry_names: list[str] = []
        if mapping is not None:
            rows += self._sync_industry(mapping.industry_name, history_days=history_days)
            industry_names.append(mapping.industry_name)
        else:
            warnings.append(f"{normalized} 暂未匹配到行业映射")

        return RelativeStrengthSyncResult(
            provider=self.provider.name,
            target_symbols=(normalized,),
            benchmark_labels=(benchmark.label,),
            industry_names=tuple(industry_names),
            mapping_rows=mapping_rows,
            rows=rows,
            warnings=tuple(warnings),
        )

    def sync_market(
        self,
        history_days: int = DEFAULT_RELATIVE_HISTORY_DAYS,
        symbols: list[str] | None = None,
    ) -> RelativeStrengthSyncResult:
        normalized_symbols = [normalize_symbol(symbol) for symbol in (symbols or [])]
        refresh_mappings = self.store.mapping_is_stale()
        if normalized_symbols and not refresh_mappings:
            refresh_mappings = any(self.store.get_symbol_industry(symbol) is None for symbol in normalized_symbols)
        warnings: list[str] = []
        if refresh_mappings:
            mapping_rows, mapping_warning = self._refresh_mappings_or_reuse_cache(normalized_symbols or None)
            if mapping_warning:
                warnings.append(mapping_warning)
        else:
            mapping_rows = 0

        mappings = self.store.list_symbol_industries(normalized_symbols or None)
        mapped_symbols = {mapping.symbol for mapping in mappings}
        warnings.extend(f"{symbol} 暂未匹配到行业映射" for symbol in normalized_symbols if symbol not in mapped_symbols)

        benchmark_labels: list[str] = []
        rows = 0
        for benchmark in available_benchmarks(normalized_symbols or None):
            rows += self._sync_benchmark(benchmark, history_days=history_days)
            benchmark_labels.append(benchmark.label)

        industry_names: list[str] = []
        seen_industries: set[str] = set()
        for mapping in mappings:
            if mapping.industry_name in seen_industries:
                continue
            seen_industries.add(mapping.industry_name)
            rows += self._sync_industry(mapping.industry_name, history_days=history_days)
            industry_names.append(mapping.industry_name)

        return RelativeStrengthSyncResult(
            provider=self.provider.name,
            target_symbols=tuple(normalized_symbols),
            benchmark_labels=tuple(benchmark_labels),
            industry_names=tuple(industry_names),
            mapping_rows=mapping_rows,
            rows=rows,
            warnings=tuple(warnings),
        )

    def _sync_mappings(self) -> int:
        timestamp = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
        mappings = [
            SymbolIndustry(
                symbol=item.symbol,
                industry_key=industry_key_from_name(item.industry_name),
                industry_name=item.industry_name,
                source=self.provider.name,
                updated_at=timestamp,
            )
            for item in self.provider.fetch_industry_mappings()
        ]
        return self.store.upsert_symbol_industries(mappings)

    def _refresh_mappings_or_reuse_cache(self, symbols: list[str] | None) -> tuple[int, str | None]:
        try:
            return self._sync_mappings(), None
        except ProviderError as exc:
            if self.store.list_symbol_industries(symbols or None):
                return 0, f"行业映射刷新失败，已回退到缓存：{exc}"
            return 0, f"行业映射刷新失败，本次仅同步指数基准：{exc}"

    def _sync_benchmark(self, benchmark: RelativeBenchmark, history_days: int) -> int:
        start_date = _sync_start_date(self.store.get_latest_date(benchmark.key), history_days)
        candles = self.provider.fetch_index_daily(benchmark.symbol, start_date=start_date)
        if not candles:
            return 0
        return self.store.upsert_candles(benchmark.key, candles)

    def _sync_industry(self, industry_name: str, history_days: int) -> int:
        industry_key = industry_key_from_name(industry_name)
        start_date = _sync_start_date(self.store.get_latest_date(industry_key), history_days)
        candles = self.provider.fetch_industry_daily(industry_name, start_date=start_date)
        if not candles:
            return 0
        return self.store.upsert_candles(industry_key, candles)


def build_relative_strength_analysis(
    symbol: str,
    candles: list[Candle],
    store: RelativeStrengthStore | None,
) -> dict[str, object]:
    if store is None or not candles:
        return _unavailable_relative_strength("相对强弱数据尚未初始化。")

    normalized = normalize_symbol(symbol)
    benchmark = benchmark_for_symbol(normalized)
    mapping = store.get_symbol_industry(normalized)
    benchmark_candles = store.get_latest(benchmark.key, limit=80)
    industry_candles = store.get_latest(mapping.industry_key, limit=80) if mapping is not None else []

    components: list[dict[str, object]] = []
    metrics: dict[str, object] = {
        "benchmarkLabel": benchmark.label,
        "benchmarkSymbol": benchmark.symbol,
        "industryName": mapping.industry_name if mapping is not None else None,
    }

    for scope, label, reference in (
        ("benchmark", benchmark.label, benchmark_candles),
        ("industry", mapping.industry_name if mapping is not None else None, industry_candles),
    ):
        if not label or not reference:
            continue
        for window in (5, 20):
            component = _build_relative_component(candles, reference, str(label), scope, window)
            if component is None:
                continue
            components.append(component)
            metrics[f"{scope}Return{window}d"] = component["referenceReturn"]
            metrics[f"stockReturn{window}d"] = component["stockReturn"]
            metrics[f"{scope}Excess{window}d"] = component["excessReturn"]

    if not components:
        return _unavailable_relative_strength(
            _missing_relative_strength_detail(benchmark_candles, mapping, industry_candles),
            benchmark_label=benchmark.label,
            benchmark_symbol=benchmark.symbol,
            industry_name=mapping.industry_name if mapping is not None else None,
        )

    score = int(round(sum(int(component["score"]) for component in components) / (len(components) * 25) * 100))
    signal, signal_label = _signal_from_relative_strength_score(score)
    detail = _build_relative_strength_detail(signal_label, metrics)
    return {
        "available": True,
        "score": score,
        "signal": signal,
        "signalLabel": signal_label,
        "detail": detail,
        "components": components,
        "metrics": metrics,
    }


def _build_relative_component(
    stock_candles: list[Candle],
    reference_candles: list[Candle],
    reference_label: str,
    scope: str,
    window: int,
) -> dict[str, object] | None:
    aligned = _aligned_closes(stock_candles, reference_candles)
    if len(aligned) < window + 1:
        return None

    stock_start = aligned[-window - 1][1]
    stock_end = aligned[-1][1]
    reference_start = aligned[-window - 1][2]
    reference_end = aligned[-1][2]
    if stock_start <= 0 or reference_start <= 0:
        return None

    stock_return = (stock_end - stock_start) / stock_start
    reference_return = (reference_end - reference_start) / reference_start
    excess_return = stock_return - reference_return
    score, verdict = _score_excess_return(excess_return)
    return {
        "label": f"{reference_label}{window}日超额",
        "score": score,
        "verdict": verdict,
        "detail": (
            f"近 {window} 日个股涨跌幅 {stock_return * 100:.2f}%，"
            f"{reference_label}{reference_return * 100:.2f}%，超额 {excess_return * 100:.2f}%。"
        ),
        "scope": scope,
        "window": window,
        "stockReturn": stock_return,
        "referenceReturn": reference_return,
        "excessReturn": excess_return,
    }


def _aligned_closes(stock_candles: list[Candle], reference_candles: list[Candle]) -> list[tuple[date, float, float]]:
    stock_by_date = {candle.date: float(candle.close) for candle in stock_candles}
    rows = [
        (candle.date, stock_by_date[candle.date], float(candle.close))
        for candle in reference_candles
        if candle.date in stock_by_date
    ]
    rows.sort(key=lambda item: item[0])
    return rows


def _score_excess_return(excess_return: float) -> tuple[int, str]:
    if excess_return >= 0.08:
        return 25, "明显跑赢"
    if excess_return >= 0.03:
        return 20, "持续跑赢"
    if excess_return >= 0.01:
        return 16, "小幅跑赢"
    if excess_return > -0.01:
        return 12, "基本同步"
    if excess_return > -0.03:
        return 8, "略弱于基准"
    if excess_return > -0.08:
        return 4, "明显弱于基准"
    return 0, "显著落后"


def _signal_from_relative_strength_score(score: int) -> tuple[str, str]:
    if score >= 65:
        return "bullish", "相对偏强"
    if score <= 35:
        return "bearish", "相对偏弱"
    return "neutral", "相对中性"


def _build_relative_strength_detail(signal_label: str, metrics: dict[str, object]) -> str:
    fragments: list[str] = []
    benchmark_label = str(metrics.get("benchmarkLabel") or "基准指数")
    industry_name = metrics.get("industryName")

    benchmark_short = metrics.get("benchmarkExcess5d")
    benchmark_medium = metrics.get("benchmarkExcess20d")
    if isinstance(benchmark_short, (int, float)):
        fragments.append(f"近 5 日相对 {benchmark_label} 超额 {benchmark_short * 100:.2f}%")
    if isinstance(benchmark_medium, (int, float)):
        fragments.append(f"近 20 日相对 {benchmark_label} 超额 {benchmark_medium * 100:.2f}%")

    industry_short = metrics.get("industryExcess5d")
    industry_medium = metrics.get("industryExcess20d")
    if industry_name and isinstance(industry_short, (int, float)):
        fragments.append(f"近 5 日相对所属行业超额 {industry_short * 100:.2f}%")
    if industry_name and isinstance(industry_medium, (int, float)):
        fragments.append(f"近 20 日相对所属行业超额 {industry_medium * 100:.2f}%")

    if not fragments:
        return "相对强弱样本不足，暂不放大结论。"
    body = "；".join(fragments)
    return f"相对强弱：{signal_label}。{body}。"


def _missing_relative_strength_detail(
    benchmark_candles: list[Candle],
    mapping: SymbolIndustry | None,
    industry_candles: list[Candle],
) -> str:
    reasons: list[str] = []
    if not benchmark_candles:
        reasons.append("指数缓存未同步")
    if mapping is None:
        reasons.append("行业映射未建立")
    elif not industry_candles:
        reasons.append(f"{mapping.industry_name} 行业K线未同步")
    if not reasons:
        reasons.append("对齐样本不足")
    joined = "、".join(reasons)
    return f"相对强弱所需的{joined}。"


def _unavailable_relative_strength(
    detail: str,
    benchmark_label: str | None = None,
    benchmark_symbol: str | None = None,
    industry_name: str | None = None,
) -> dict[str, object]:
    return {
        "available": False,
        "score": 50,
        "signal": "neutral",
        "signalLabel": "待同步",
        "detail": detail,
        "components": [],
        "metrics": {
            "benchmarkLabel": benchmark_label,
            "benchmarkSymbol": benchmark_symbol,
            "industryName": industry_name,
        },
    }


def _sync_start_date(latest_date: date | None, history_days: int) -> date:
    if latest_date is not None:
        return latest_date + timedelta(days=1)
    return date.today() - timedelta(days=max(45, history_days * 3))


def _latest_timestamp(*values: str | None) -> str | None:
    latest_value: str | None = None
    latest_moment: datetime | None = None
    for value in values:
        if not value:
            continue
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            moment = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if latest_moment is None or moment > latest_moment:
            latest_moment = moment
            latest_value = value
    return latest_value
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from .providers import ProviderError
from .storage import normalize_symbol


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_FUND_HISTORY_DAYS = 15
FUND_SIGNAL_LABELS = {
    "bullish": "偏多",
    "neutral": "中性",
    "bearish": "偏空",
}


@dataclass(frozen=True)
class FundFactor:
    symbol: str
    trade_date: date
    fund_net_inflow: float | None = None
    fund_net_inflow_ratio: float | None = None
    margin_balance: float | None = None
    margin_buy_amount: float | None = None
    source: str = "akshare"
    updated_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "tradeDate": self.trade_date.isoformat(),
            "fundNetInflow": self.fund_net_inflow,
            "fundNetInflowRatio": self.fund_net_inflow_ratio,
            "marginBalance": self.margin_balance,
            "marginBuyAmount": self.margin_buy_amount,
            "source": self.source,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True)
class FundSyncResult:
    trade_date: date
    provider: str
    rows: int

    def to_dict(self) -> dict[str, object]:
        return {
            "tradeDate": self.trade_date.isoformat(),
            "provider": self.provider,
            "rows": self.rows,
        }


@dataclass(frozen=True)
class FundSyncWindowResult:
    target_date: date
    requested_days: int
    synced_trade_dates: tuple[date, ...]
    skipped_trade_dates: tuple[date, ...]
    rows: int
    providers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "targetDate": self.target_date.isoformat(),
            "requestedDays": self.requested_days,
            "syncedDays": len(self.synced_trade_dates),
            "skippedDays": len(self.skipped_trade_dates),
            "syncedTradeDates": [value.isoformat() for value in self.synced_trade_dates],
            "skippedTradeDates": [value.isoformat() for value in self.skipped_trade_dates],
            "rows": self.rows,
            "providers": list(self.providers),
        }


class FundFactorProvider(Protocol):
    name: str

    def fetch_latest(self, trade_date: date) -> list[FundFactor]:
        raise NotImplementedError


@dataclass
class MemoryFundFactorProvider:
    name: str
    factors: list[FundFactor] | None = None
    error: Exception | None = None

    def fetch_latest(self, trade_date: date) -> list[FundFactor]:
        if self.error is not None:
            raise self.error
        return [factor for factor in list(self.factors or []) if factor.trade_date == trade_date]


class FundFactorStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS fund_factors (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    fund_net_inflow REAL,
                    fund_net_inflow_ratio REAL,
                    margin_balance REAL,
                    margin_buy_amount REAL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, trade_date)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fund_factors_symbol_trade_date ON fund_factors(symbol, trade_date)"
            )

    def upsert_many(self, factors: list[FundFactor]) -> int:
        if not factors:
            return 0

        now_text = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
        rows = [
            (
                normalize_symbol(factor.symbol),
                factor.trade_date.isoformat(),
                factor.fund_net_inflow,
                factor.fund_net_inflow_ratio,
                factor.margin_balance,
                factor.margin_buy_amount,
                factor.source,
                factor.updated_at or now_text,
            )
            for factor in factors
        ]

        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO fund_factors(
                    symbol,
                    trade_date,
                    fund_net_inflow,
                    fund_net_inflow_ratio,
                    margin_balance,
                    margin_buy_amount,
                    source,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    fund_net_inflow = excluded.fund_net_inflow,
                    fund_net_inflow_ratio = excluded.fund_net_inflow_ratio,
                    margin_balance = excluded.margin_balance,
                    margin_buy_amount = excluded.margin_buy_amount,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get_latest(self, symbol: str, limit: int = 5) -> list[FundFactor]:
        if limit <= 0:
            return []
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT symbol, trade_date, fund_net_inflow, fund_net_inflow_ratio, margin_balance, margin_buy_amount, source, updated_at
                FROM fund_factors
                WHERE symbol = ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (normalize_symbol(symbol), limit),
            ).fetchall()
        factors = [_row_to_factor(row) for row in rows]
        factors.reverse()
        return factors

    def get_latest_trade_date(self) -> date | None:
        with self._connection() as conn:
            row = conn.execute("SELECT MAX(trade_date) AS latest_trade_date FROM fund_factors").fetchone()
        if row is None or row["latest_trade_date"] is None:
            return None
        return date.fromisoformat(row["latest_trade_date"])

    def get_trade_dates(self, start_date: date | None = None, end_date: date | None = None) -> list[date]:
        clauses: list[str] = []
        params: list[str] = []
        if start_date is not None:
            clauses.append("trade_date >= ?")
            params.append(start_date.isoformat())
        if end_date is not None:
            clauses.append("trade_date <= ?")
            params.append(end_date.isoformat())

        sql = "SELECT DISTINCT trade_date FROM fund_factors"
        if clauses:
            sql = f"{sql} WHERE {' AND '.join(clauses)}"
        sql = f"{sql} ORDER BY trade_date"

        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [date.fromisoformat(str(row["trade_date"])) for row in rows]

    def merge_from(self, source_db_path: str | Path) -> int:
        source_path = Path(source_db_path)
        if not source_path.exists():
            return 0

        with self._connection() as conn:
            conn.execute("ATTACH DATABASE ? AS source_db", (str(source_path),))
            try:
                row = conn.execute(
                    """
                    SELECT name
                    FROM source_db.sqlite_master
                    WHERE type = 'table' AND name = 'fund_factors'
                    """
                ).fetchone()
                if row is None:
                    return 0

                total = conn.execute("SELECT COUNT(*) AS total FROM source_db.fund_factors").fetchone()["total"]
                conn.execute(
                    """
                    INSERT OR REPLACE INTO fund_factors(
                        symbol,
                        trade_date,
                        fund_net_inflow,
                        fund_net_inflow_ratio,
                        margin_balance,
                        margin_buy_amount,
                        source,
                        updated_at
                    )
                    SELECT
                        symbol,
                        trade_date,
                        fund_net_inflow,
                        fund_net_inflow_ratio,
                        margin_balance,
                        margin_buy_amount,
                        source,
                        updated_at
                    FROM source_db.fund_factors
                    """
                )
                conn.commit()
            finally:
                conn.execute("DETACH DATABASE source_db")
        return int(total)


class AkShareFundFactorProvider:
    name = "akshare"

    def fetch_latest(self, trade_date: date) -> list[FundFactor]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("akshare is not installed") from exc

        errors: list[str] = []
        flow_map = self._fetch_fund_flow_map(ak, errors)
        margin_map = self._fetch_margin_map(ak, trade_date, errors)

        all_symbols = sorted(set(flow_map) | set(margin_map))
        if not all_symbols:
            raise ProviderError("; ".join(errors) if errors else "fund providers returned no rows")

        factors: list[FundFactor] = []
        for symbol in all_symbols:
            if not _is_supported_a_share_symbol(symbol):
                continue
            flow_payload = flow_map.get(symbol, {})
            margin_payload = margin_map.get(symbol, {})
            if not flow_payload and not margin_payload:
                continue
            factors.append(
                FundFactor(
                    symbol=symbol,
                    trade_date=trade_date,
                    fund_net_inflow=flow_payload.get("fund_net_inflow"),
                    fund_net_inflow_ratio=flow_payload.get("fund_net_inflow_ratio"),
                    margin_balance=margin_payload.get("margin_balance"),
                    margin_buy_amount=margin_payload.get("margin_buy_amount"),
                    source="akshare",
                )
            )
        if not factors:
            raise ProviderError("; ".join(errors) if errors else "fund providers returned no A-share rows")
        return factors

    def _fetch_fund_flow_map(self, ak: object, errors: list[str]) -> dict[str, dict[str, float | None]]:
        flow_map: dict[str, dict[str, float | None]] = {}

        try:
            ratio_frame = ak.stock_main_fund_flow(symbol="全部股票")
            if ratio_frame is not None and not ratio_frame.empty:
                for _, row in ratio_frame.iterrows():
                    symbol = _normalize_provider_symbol(row.get("代码", ""))
                    if not _is_supported_a_share_symbol(symbol):
                        continue
                    flow_map[symbol] = {
                        **flow_map.get(symbol, {}),
                        "fund_net_inflow_ratio": _to_float(row.get("今日排行榜-主力净占比")),
                    }
        except Exception as exc:
            errors.append(f"stock_main_fund_flow: {exc}")

        try:
            amount_frame = ak.stock_fund_flow_individual(symbol="即时")
            if amount_frame is None or amount_frame.empty:
                raise ProviderError("returned no rows")
            for _, row in amount_frame.iterrows():
                symbol = _normalize_provider_symbol(row.get("股票代码", ""))
                if not _is_supported_a_share_symbol(symbol):
                    continue
                net_inflow = _parse_chinese_number(row.get("净额"))
                turnover_amount = _parse_chinese_number(row.get("成交额"))
                fund_ratio = flow_map.get(symbol, {}).get("fund_net_inflow_ratio")
                if fund_ratio is None and turnover_amount not in (None, 0) and net_inflow is not None:
                    fund_ratio = round((net_inflow / turnover_amount) * 100, 4)
                flow_map[symbol] = {
                    **flow_map.get(symbol, {}),
                    "fund_net_inflow": net_inflow,
                    "fund_net_inflow_ratio": fund_ratio,
                }
        except Exception as exc:
            errors.append(f"stock_fund_flow_individual: {exc}")

        return flow_map

    def _fetch_margin_map(self, ak: object, trade_date: date, errors: list[str]) -> dict[str, dict[str, float | None]]:
        margin_map: dict[str, dict[str, float | None]] = {}
        compact_date = trade_date.strftime("%Y%m%d")

        exchange_loaders = [
            (
                ak.stock_margin_detail_sse,
                compact_date,
                "标的证券代码",
                "融资余额",
                "融资买入额",
                "stock_margin_detail_sse",
            ),
            (
                ak.stock_margin_detail_szse,
                compact_date,
                "证券代码",
                "融资余额",
                "融资买入额",
                "stock_margin_detail_szse",
            ),
        ]

        for loader, arg, symbol_column, margin_balance_column, margin_buy_column, label in exchange_loaders:
            try:
                frame = loader(date=arg)
                if frame is None or frame.empty:
                    continue
                for _, row in frame.iterrows():
                    symbol = _normalize_provider_symbol(row.get(symbol_column, ""))
                    if not _is_supported_a_share_symbol(symbol):
                        continue
                    margin_map[symbol] = {
                        **margin_map.get(symbol, {}),
                        "margin_balance": _to_float(row.get(margin_balance_column)),
                        "margin_buy_amount": _to_float(row.get(margin_buy_column)),
                    }
            except Exception as exc:
                errors.append(f"{label}: {exc}")

        return margin_map


class FundFactorSyncService:
    def __init__(self, store: FundFactorStore, providers: list[FundFactorProvider]):
        self.store = store
        self.providers = providers

    def sync_latest(self, trade_date: date | None = None) -> FundSyncResult:
        target_date = trade_date or latest_a_share_trade_date()
        errors: list[str] = []
        for provider in self.providers:
            try:
                factors = provider.fetch_latest(target_date)
                if not factors:
                    raise ProviderError("returned no rows")
                rows = self.store.upsert_many(factors)
                return FundSyncResult(trade_date=target_date, provider=provider.name, rows=rows)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        raise ProviderError("; ".join(errors) if errors else "no fund providers configured")

    def sync_recent(self, history_days: int = DEFAULT_FUND_HISTORY_DAYS, trade_date: date | None = None) -> FundSyncWindowResult:
        if history_days <= 0:
            raise ValueError("history_days must be > 0")

        target_date = trade_date or latest_a_share_trade_date()
        window_dates = recent_a_share_trade_dates(history_days, end_date=target_date)
        if not window_dates:
            raise ProviderError("failed to resolve recent A-share trading sessions")

        existing_dates = set(self.store.get_trade_dates(start_date=window_dates[0], end_date=window_dates[-1]))
        pending_dates = [value for value in window_dates if value not in existing_dates]
        if target_date not in pending_dates:
            pending_dates.append(target_date)
        pending_dates = sorted(set(pending_dates))

        results = [self.sync_latest(trade_date=value) for value in pending_dates]
        synced_dates = tuple(result.trade_date for result in results)
        skipped_dates = tuple(value for value in window_dates if value not in synced_dates)
        providers = tuple(dict.fromkeys(result.provider for result in results))

        return FundSyncWindowResult(
            target_date=target_date,
            requested_days=history_days,
            synced_trade_dates=synced_dates,
            skipped_trade_dates=skipped_dates,
            rows=sum(result.rows for result in results),
            providers=providers,
        )


def build_default_fund_providers() -> list[FundFactorProvider]:
    names = [name.strip().lower() for name in os.getenv("FUND_DATA_PROVIDERS", "akshare").split(",") if name.strip()]
    providers: list[FundFactorProvider] = []
    for name in names:
        if name == "akshare":
            providers.append(AkShareFundFactorProvider())
    return providers


def latest_a_share_trade_date(now: datetime | None = None) -> date:
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise ProviderError("exchange_calendars is not installed") from exc

    reference = now.astimezone(SHANGHAI_TZ) if now is not None else datetime.now(SHANGHAI_TZ)
    calendar = xcals.get_calendar("XSHG")
    end = pd.Timestamp(reference.date())
    start = end - pd.Timedelta(days=14)
    sessions = calendar.sessions_in_range(start, end)
    if len(sessions) == 0:
        raise ProviderError("failed to resolve latest A-share trading session")
    latest = sessions[-1].date()
    if latest == reference.date() and reference.timetz() < time(hour=16, minute=30, tzinfo=SHANGHAI_TZ):
        if len(sessions) >= 2:
            latest = sessions[-2].date()
    return latest


def recent_a_share_trade_dates(days: int, end_date: date | None = None) -> list[date]:
    if days <= 0:
        return []

    target = end_date or latest_a_share_trade_date()
    start = target - timedelta(days=max(days * 5, 60))
    return a_share_trade_dates_between(start, target)[-days:]


def a_share_trade_dates_between(start_date: date, end_date: date) -> list[date]:
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise ProviderError("exchange_calendars is not installed") from exc

    calendar = xcals.get_calendar("XSHG")
    sessions = calendar.sessions_in_range(pd.Timestamp(start_date), pd.Timestamp(end_date))
    return [session.date() for session in sessions]


def build_fund_analysis(factors: list[FundFactor]) -> dict[str, object]:
    if not factors:
        raise ProviderError("fund factors are empty")

    ordered = sorted(factors, key=lambda item: item.trade_date)
    latest = ordered[-1]
    previous = ordered[-2] if len(ordered) >= 2 else None

    flow_metrics = {
        "netInflow3d": _rolling_sum(ordered, "fund_net_inflow", 3),
        "netInflow5d": _rolling_sum(ordered, "fund_net_inflow", 5),
        "netInflow10d": _rolling_sum(ordered, "fund_net_inflow", 10),
        "consecutiveInflowDays": _count_consecutive_flow_days(ordered, direction="inflow"),
    }
    margin_metrics = {
        "balanceSlope3d": _slope_ratio(ordered, "margin_balance", 3),
        "balanceAcceleration3d": _acceleration_ratio(ordered, "margin_balance", 3),
    }
    trend_profile = _classify_trend_profile(ordered, flow_metrics, margin_metrics)

    components = [
        _fund_net_inflow_component(latest.fund_net_inflow),
        _fund_ratio_component(latest.fund_net_inflow_ratio),
        _cumulative_flow_component(flow_metrics),
        _consecutive_inflow_component(int(flow_metrics["consecutiveInflowDays"] or 0)),
        _margin_trend_component(margin_metrics),
    ]
    score = int(round(sum(component["score"] for component in components) / (len(components) * 25) * 100))
    signal = _signal_from_score(score)
    signal_label = FUND_SIGNAL_LABELS[signal]
    summary = _build_fund_summary(latest, previous, signal_label, flow_metrics, margin_metrics, trend_profile)

    return {
        "tradeDate": latest.trade_date.isoformat(),
        "historyDays": len(ordered),
        "score": score,
        "signal": signal,
        "signalLabel": signal_label,
        "summary": summary,
        "latest": latest.to_dict(),
        "flowMetrics": flow_metrics,
        "marginMetrics": margin_metrics,
        "trendProfile": trend_profile,
        "changes": {
            "marginBalanceChange": _delta_ratio(latest.margin_balance, previous.margin_balance if previous else None),
            "marginBuyAmountChange": _delta_ratio(latest.margin_buy_amount, previous.margin_buy_amount if previous else None),
        },
        "components": components,
    }


def _row_to_factor(row: sqlite3.Row) -> FundFactor:
    return FundFactor(
        symbol=normalize_symbol(str(row["symbol"])),
        trade_date=date.fromisoformat(row["trade_date"]),
        fund_net_inflow=_to_float(row["fund_net_inflow"]),
        fund_net_inflow_ratio=_to_float(row["fund_net_inflow_ratio"]),
        margin_balance=_to_float(row["margin_balance"]),
        margin_buy_amount=_to_float(row["margin_buy_amount"]),
        source=str(row["source"]),
        updated_at=str(row["updated_at"]),
    )


def _is_supported_a_share_symbol(symbol: str) -> bool:
    code = normalize_symbol(symbol)
    if len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(("000", "001", "002", "003", "300", "301", "430", "600", "601", "603", "605", "688", "689", "8", "92"))


def _normalize_provider_symbol(value: object) -> str:
    if value is None or pd.isna(value):
        return ""

    text = str(value).strip()
    if not text or text.lower() in {"none", "nan"}:
        return ""

    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]

    normalized = normalize_symbol(text)
    if normalized.isdigit() and len(normalized) < 6:
        normalized = normalized.zfill(6)
    return normalized


def _parse_chinese_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "None", "nan", "NaN"}:
        return None
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier = 100000000.0
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 10000.0
        text = text[:-1]
    elif text.endswith("%"):
        text = text[:-1]
    return float(text) * multiplier


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "None", "nan", "NaN"}:
        return None
    if text.endswith("%"):
        text = text[:-1]
    return float(text)


def _fund_net_inflow_component(value: float | None) -> dict[str, object]:
    if value is None:
        return {"label": "单日主力净额", "score": 12, "value": None, "verdict": "缺少数据", "detail": "最新交易日缺少主力净额，暂时只能参考其他指标。"}
    if value > 0:
        score = 25 if value >= 100000000 else 18
        verdict = "净流入"
    elif value < 0:
        score = 0 if value <= -100000000 else 6
        verdict = "净流出"
    else:
        score = 12
        verdict = "持平"
    return {
        "label": "单日主力净额",
        "score": score,
        "value": value,
        "verdict": verdict,
        "detail": f"最新交易日主力资金为{verdict}。",
    }


def _fund_ratio_component(value: float | None) -> dict[str, object]:
    if value is None:
        return {"label": "单日净流入占比", "score": 12, "value": None, "verdict": "缺少数据", "detail": "最新交易日缺少净流入占比，短线强度判断会更保守。"}
    if value >= 5:
        score, verdict = 25, "强势流入"
    elif value >= 2:
        score, verdict = 20, "偏强流入"
    elif value >= 0:
        score, verdict = 14, "温和流入"
    elif value > -2:
        score, verdict = 8, "小幅流出"
    else:
        score, verdict = 0, "明显流出"
    return {
        "label": "单日净流入占比",
        "score": score,
        "value": value,
        "verdict": verdict,
        "detail": f"最新交易日净流入占比判断为{verdict}。",
    }


def _trend_component(label: str, latest: float | None, previous: float | None) -> dict[str, object]:
    change_ratio = _delta_ratio(latest, previous)
    if change_ratio is None:
        return {"label": label, "score": 12, "value": latest, "verdict": "缺少趋势数据"}
    if change_ratio >= 0.02:
        score, verdict = 25, "明显增强"
    elif change_ratio >= 0.005:
        score, verdict = 18, "温和增强"
    elif change_ratio > -0.005:
        score, verdict = 12, "基本持平"
    elif change_ratio > -0.02:
        score, verdict = 6, "小幅走弱"
    else:
        score, verdict = 0, "明显走弱"
    return {"label": label, "score": score, "value": latest, "changeRatio": change_ratio, "verdict": verdict}


def _delta_ratio(latest: float | None, previous: float | None) -> float | None:
    if latest is None or previous in (None, 0):
        return None
    return round((latest - previous) / abs(previous), 6)


def _signal_from_score(score: int) -> str:
    if score >= 70:
        return "bullish"
    if score >= 40:
        return "neutral"
    return "bearish"


def _build_fund_summary(
    latest: FundFactor,
    previous: FundFactor | None,
    signal_label: str,
    flow_metrics: dict[str, object],
    margin_metrics: dict[str, object],
    trend_profile: dict[str, str],
) -> str:
    flow_part = "单日主力资金缺少数据"
    if latest.fund_net_inflow is not None:
        flow_part = "最新一天主力资金净流入" if latest.fund_net_inflow > 0 else "最新一天主力资金净流出"

    profile_part = f"当前更像{trend_profile['label']}"
    if trend_profile.get("detail"):
        profile_part = f"{profile_part}，{trend_profile['detail']}"

    cumulative_flow_parts: list[str] = []
    for label, value in (("3日", flow_metrics.get("netInflow3d")), ("5日", flow_metrics.get("netInflow5d")), ("10日", flow_metrics.get("netInflow10d"))):
        if value is not None:
            cumulative_flow_parts.append(f"{label}累计主力净额{_format_money_short(float(value))}")
    cumulative_flow = "；".join(cumulative_flow_parts) if cumulative_flow_parts else "多日累计主力净额仍在积累中"

    consecutive_days = int(flow_metrics.get("consecutiveInflowDays") or 0)
    consecutive_part = f"已连续净流入 {consecutive_days} 日" if consecutive_days > 0 else "最近没有形成连续净流入"

    margin_slope = margin_metrics.get("balanceSlope3d")
    margin_part = "融资余额 3 日趋势待补充"
    if margin_slope is not None:
        margin_part = "融资余额 3 日斜率向上" if float(margin_slope) > 0 else "融资余额 3 日斜率向下"
    elif previous is not None and latest.margin_balance is not None:
        margin_part = "融资余额有值，但 3 日斜率样本还不够"

    return f"资金面结论：{signal_label}。{flow_part}；{profile_part}。{cumulative_flow}；{consecutive_part}；{margin_part}。"


def _rolling_sum(factors: list[FundFactor], field: str, length: int) -> float | None:
    values = _recent_values(factors, field, length)
    if values is None:
        return None
    return round(sum(values), 2)


def _recent_values(factors: list[FundFactor], field: str, length: int) -> list[float] | None:
    if len(factors) < length:
        return None
    window = factors[-length:]
    values: list[float] = []
    for item in window:
        value = getattr(item, field)
        if value is None:
            return None
        values.append(float(value))
    return values


def _count_consecutive_flow_days(factors: list[FundFactor], direction: str = "inflow") -> int:
    count = 0
    sign = 1 if direction == "inflow" else -1
    for item in reversed(factors):
        value = item.fund_net_inflow
        if value is None or value == 0:
            break
        if sign > 0 and value > 0:
            count += 1
            continue
        if sign < 0 and value < 0:
            count += 1
            continue
        break
    return count


def _slope_ratio(factors: list[FundFactor], field: str, length: int) -> float | None:
    values = _recent_values(factors, field, length)
    if values is None or values[0] == 0:
        return None
    return round((values[-1] - values[0]) / abs(values[0]), 6)


def _acceleration_ratio(factors: list[FundFactor], field: str, length: int) -> float | None:
    values = _recent_values(factors, field, length)
    if values is None or len(values) < 3 or values[0] == 0 or values[1] == 0:
        return None
    first_leg = (values[1] - values[0]) / abs(values[0])
    second_leg = (values[2] - values[1]) / abs(values[1])
    return round(second_leg - first_leg, 6)


def _cumulative_flow_component(flow_metrics: dict[str, object]) -> dict[str, object]:
    net_3d = flow_metrics.get("netInflow3d")
    net_5d = flow_metrics.get("netInflow5d")
    net_10d = flow_metrics.get("netInflow10d")
    available = [value for value in (net_3d, net_5d, net_10d) if value is not None]
    if not available:
        return {
            "label": "多日累计主力净流入",
            "score": 12,
            "verdict": "样本不足",
            "detail": "3 日、5 日、10 日累计主力净流入还没有足够样本。",
        }

    positive = sum(1 for value in available if float(value) > 0)
    negative = sum(1 for value in available if float(value) < 0)
    if positive == len(available):
        score = 25 if len(available) >= 2 else 18
        verdict = "多日持续净流入"
    elif negative == len(available):
        score = 0 if len(available) >= 2 else 6
        verdict = "多日持续净流出"
    elif net_3d is not None and float(net_3d) > 0:
        score = 14
        verdict = "短线回暖"
    else:
        score = 10
        verdict = "方向分化"

    detail = " / ".join(
        f"{label}{_format_money_short(float(value))}"
        for label, value in (("3日", net_3d), ("5日", net_5d), ("10日", net_10d))
        if value is not None
    )
    return {
        "label": "多日累计主力净流入",
        "score": score,
        "verdict": verdict,
        "detail": f"{verdict}。{detail}。",
    }


def _consecutive_inflow_component(days: int) -> dict[str, object]:
    if days <= 0:
        return {
            "label": "连续净流入天数",
            "score": 4,
            "value": days,
            "verdict": "没有连续净流入",
            "detail": "最近一个交易日没有延续主力净流入。",
        }
    if days == 1:
        return {
            "label": "连续净流入天数",
            "score": 12,
            "value": days,
            "verdict": "单日净流入",
            "detail": "只有最新一天净流入，更接近单日异动。",
        }
    if days <= 3:
        return {
            "label": "连续净流入天数",
            "score": 18,
            "value": days,
            "verdict": "短线延续",
            "detail": f"主力资金已连续净流入 {days} 个交易日。",
        }
    return {
        "label": "连续净流入天数",
        "score": 25,
        "value": days,
        "verdict": "持续流入",
        "detail": f"主力资金已连续净流入 {days} 个交易日，持续性较强。",
    }


def _margin_trend_component(margin_metrics: dict[str, object]) -> dict[str, object]:
    slope = margin_metrics.get("balanceSlope3d")
    acceleration = margin_metrics.get("balanceAcceleration3d")
    if slope is None:
        return {
            "label": "融资余额 3 日趋势",
            "score": 12,
            "verdict": "样本不足",
            "detail": "融资余额 3 日斜率和加速度还没有足够样本。",
        }

    slope_value = float(slope)
    acceleration_value = float(acceleration or 0)
    if slope_value >= 0.02 and acceleration_value >= 0:
        score, verdict = 25, "融资趋势明显增强"
    elif slope_value > 0:
        score, verdict = 18, "融资趋势回升"
    elif slope_value > -0.01:
        score, verdict = 12, "融资趋势基本持平"
    elif acceleration_value >= 0:
        score, verdict = 6, "融资趋势回落但在放缓"
    else:
        score, verdict = 0, "融资趋势继续走弱"

    return {
        "label": "融资余额 3 日趋势",
        "score": score,
        "verdict": verdict,
        "detail": f"{verdict}。3 日斜率 {_format_ratio_percent(slope_value)}，加速度 {_format_ratio_percent(acceleration_value)}。",
    }


def _classify_trend_profile(
    factors: list[FundFactor],
    flow_metrics: dict[str, object],
    margin_metrics: dict[str, object],
) -> dict[str, str]:
    latest_flow = factors[-1].fund_net_inflow
    net_3d = flow_metrics.get("netInflow3d")
    net_5d = flow_metrics.get("netInflow5d")
    net_10d = flow_metrics.get("netInflow10d")
    consecutive_inflow_days = int(flow_metrics.get("consecutiveInflowDays") or 0)
    consecutive_outflow_days = _count_consecutive_flow_days(factors, direction="outflow")
    positive_windows = sum(1 for value in (net_3d, net_5d, net_10d) if value is not None and float(value) > 0)
    negative_windows = sum(1 for value in (net_3d, net_5d, net_10d) if value is not None and float(value) < 0)
    margin_slope = margin_metrics.get("balanceSlope3d")

    if latest_flow is not None and latest_flow > 0 and positive_windows >= 2 and consecutive_inflow_days >= 2:
        detail = "近 3 日到 10 日累计净流入已经和单日方向同向"
        if margin_slope is not None and float(margin_slope) > 0:
            detail = f"{detail}，融资余额也在回升"
        return {"code": "sustained_trend", "label": "持续趋势", "detail": detail}

    if latest_flow is not None and latest_flow < 0 and negative_windows >= 2 and consecutive_outflow_days >= 2:
        return {"code": "sustained_trend", "label": "持续趋势", "detail": "近 3 日到 10 日累计净流出延续，短线承压仍在持续"}

    if latest_flow is not None and latest_flow > 0 and consecutive_inflow_days <= 1 and (negative_windows >= 1 or positive_windows == 0):
        return {"code": "single_day_anomaly", "label": "单日异动", "detail": "最新一天资金回流，但多日累计资金还没形成持续共振"}

    if latest_flow is not None and latest_flow < 0 and negative_windows <= 1:
        return {"code": "single_day_anomaly", "label": "单日异动", "detail": "最新一天资金转弱，但多日累计趋势还没有完全走坏"}

    return {"code": "mixed_signal", "label": "趋势未明", "detail": "单日资金和多日累计方向还没有形成稳定结论"}


def _format_money_short(value: float) -> str:
    abs_value = abs(value)
    sign = "-" if value < 0 else ""
    if abs_value >= 100000000:
        return f"{sign}{abs_value / 100000000:.2f}亿"
    if abs_value >= 10000:
        return f"{sign}{abs_value / 10000:.2f}万"
    return f"{value:.0f}"


def _format_ratio_percent(value: float) -> str:
    return f"{value * 100:+.2f}%"
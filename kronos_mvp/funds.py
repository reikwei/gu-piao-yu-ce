from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from .providers import ProviderError
from .storage import normalize_symbol


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
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
                    symbol = normalize_symbol(str(row.get("代码", "")))
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
                symbol = normalize_symbol(str(row.get("股票代码", "")))
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
                    symbol = normalize_symbol(str(row.get(symbol_column, "")))
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


def build_fund_analysis(factors: list[FundFactor]) -> dict[str, object]:
    if not factors:
        raise ProviderError("fund factors are empty")

    ordered = sorted(factors, key=lambda item: item.trade_date)
    latest = ordered[-1]
    previous = ordered[-2] if len(ordered) >= 2 else None

    components = [
        _fund_net_inflow_component(latest.fund_net_inflow),
        _fund_ratio_component(latest.fund_net_inflow_ratio),
        _trend_component("融资余额趋势", latest.margin_balance, previous.margin_balance if previous else None),
        _trend_component("融资买入额趋势", latest.margin_buy_amount, previous.margin_buy_amount if previous else None),
    ]
    score = int(round(sum(component["score"] for component in components)))
    signal = _signal_from_score(score)
    signal_label = FUND_SIGNAL_LABELS[signal]
    summary = _build_fund_summary(latest, previous, signal_label)

    return {
        "tradeDate": latest.trade_date.isoformat(),
        "historyDays": len(ordered),
        "score": score,
        "signal": signal,
        "signalLabel": signal_label,
        "summary": summary,
        "latest": latest.to_dict(),
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
        return {"label": "资金净额", "score": 12, "value": None, "verdict": "缺少数据"}
    if value > 0:
        score = 25 if value >= 100000000 else 18
        verdict = "净流入"
    elif value < 0:
        score = 0 if value <= -100000000 else 6
        verdict = "净流出"
    else:
        score = 12
        verdict = "持平"
    return {"label": "资金净额", "score": score, "value": value, "verdict": verdict}


def _fund_ratio_component(value: float | None) -> dict[str, object]:
    if value is None:
        return {"label": "资金净流入占比", "score": 12, "value": None, "verdict": "缺少数据"}
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
    return {"label": "资金净流入占比", "score": score, "value": value, "verdict": verdict}


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


def _build_fund_summary(latest: FundFactor, previous: FundFactor | None, signal_label: str) -> str:
    flow_part = "资金净额缺少数据"
    if latest.fund_net_inflow is not None:
        flow_part = "资金净流入增强" if latest.fund_net_inflow > 0 else "资金净流出偏强"

    leverage_part = "融资趋势待补充"
    margin_change = _delta_ratio(latest.margin_balance, previous.margin_balance if previous else None)
    if margin_change is not None:
        leverage_part = "融资余额回升" if margin_change > 0 else "融资余额回落"

    return f"资金面结论：{signal_label}。{flow_part}，{leverage_part}。"
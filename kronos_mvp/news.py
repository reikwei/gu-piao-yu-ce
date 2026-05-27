from __future__ import annotations

import hashlib
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from .providers import ProviderError
from .storage import normalize_symbol


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_NEWS_LOOKBACK_DAYS = 3
DEFAULT_NEWS_ITEMS_LIMIT = 8


_NEWS_SIGNAL_LABELS = {
    "bullish": "偏多",
    "neutral": "中性",
    "bearish": "偏空",
}

_POSITIVE_TERMS = (
    "增持",
    "回购",
    "中标",
    "签约",
    "业绩预增",
    "业绩大增",
    "净利润增长",
    "超预期",
    "利好",
    "获批",
    "突破",
    "上调",
    "龙头",
)

_NEGATIVE_TERMS = (
    "减持",
    "清仓",
    "业绩预亏",
    "亏损",
    "暴跌",
    "下调",
    "处罚",
    "立案",
    "诉讼",
    "违约",
    "风险",
    "商誉减值",
    "退市",
    "问询",
)


@dataclass(frozen=True)
class StockNewsItem:
    symbol: str
    title: str
    summary: str
    published_at: str
    source: str
    url: str
    sentiment_score: float
    sentiment_label: str
    updated_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "title": self.title,
            "summary": self.summary,
            "publishedAt": self.published_at,
            "source": self.source,
            "url": self.url,
            "sentimentScore": self.sentiment_score,
            "sentimentLabel": self.sentiment_label,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True)
class StockNewsSyncResult:
    symbol: str
    provider: str
    rows: int

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "provider": self.provider,
            "rows": self.rows,
        }


class StockNewsProvider(Protocol):
    name: str

    def fetch_latest(self, symbol: str, limit: int = 50) -> list[StockNewsItem]:
        raise NotImplementedError


class StockNewsStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
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
                CREATE TABLE IF NOT EXISTS stock_news (
                    symbol TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    sentiment_score REAL NOT NULL,
                    sentiment_label TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, url)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_stock_news_symbol_published ON stock_news(symbol, published_at DESC)"
            )

    def upsert_many(self, items: list[StockNewsItem]) -> int:
        if not items:
            return 0
        now_text = datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
        rows = [
            (
                normalize_symbol(item.symbol),
                item.url,
                item.title,
                item.summary,
                item.source,
                item.published_at,
                float(item.sentiment_score),
                item.sentiment_label,
                item.updated_at or now_text,
            )
            for item in items
        ]
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO stock_news(
                    symbol,
                    url,
                    title,
                    summary,
                    source,
                    published_at,
                    sentiment_score,
                    sentiment_label,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, url) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    source = excluded.source,
                    published_at = excluded.published_at,
                    sentiment_score = excluded.sentiment_score,
                    sentiment_label = excluded.sentiment_label,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get_latest(self, symbol: str, limit: int = DEFAULT_NEWS_ITEMS_LIMIT, max_age_days: int = DEFAULT_NEWS_LOOKBACK_DAYS) -> list[StockNewsItem]:
        if limit <= 0:
            return []
        normalized = normalize_symbol(symbol)
        cutoff = datetime.now(SHANGHAI_TZ) - timedelta(days=max(1, max_age_days))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT symbol, url, title, summary, source, published_at, sentiment_score, sentiment_label, updated_at
                FROM stock_news
                WHERE symbol = ? AND published_at >= ?
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (normalized, cutoff.isoformat(timespec="seconds"), limit),
            ).fetchall()
        return [
            StockNewsItem(
                symbol=str(row["symbol"]),
                url=str(row["url"]),
                title=str(row["title"]),
                summary=str(row["summary"]),
                source=str(row["source"]),
                published_at=str(row["published_at"]),
                sentiment_score=float(row["sentiment_score"]),
                sentiment_label=str(row["sentiment_label"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def get_symbol_last_updated_at(self, symbol: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) AS latest_updated_at FROM stock_news WHERE symbol = ?",
                (normalize_symbol(symbol),),
            ).fetchone()
        if row is None or row["latest_updated_at"] is None:
            return None
        return str(row["latest_updated_at"])

    def get_last_updated_at(self) -> str | None:
        with self._connection() as conn:
            row = conn.execute("SELECT MAX(updated_at) AS latest_updated_at FROM stock_news").fetchone()
        if row is None or row["latest_updated_at"] is None:
            return None
        return str(row["latest_updated_at"])


class AkShareStockNewsProvider:
    name = "akshare"

    def fetch_latest(self, symbol: str, limit: int = 50) -> list[StockNewsItem]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("akshare is not installed") from exc

        code = normalize_symbol(symbol)
        try:
            frame = ak.stock_news_em(symbol=code)
        except Exception as exc:
            raise ProviderError(str(exc)) from exc

        if frame is None or frame.empty:
            return []

        title_col = _find_column(frame.columns, ("新闻标题", "标题", "title"))
        summary_col = _find_column(frame.columns, ("新闻内容", "摘要", "内容", "content"))
        source_col = _find_column(frame.columns, ("文章来源", "来源", "source"))
        url_col = _find_column(frame.columns, ("新闻链接", "链接", "url"))
        published_col = _find_column(frame.columns, ("发布时间", "时间", "publish_time", "date"))

        if title_col is None:
            raise ProviderError("akshare stock_news_em schema changed: missing title column")

        items: list[StockNewsItem] = []
        for _, row in frame.head(max(1, limit)).iterrows():
            title = _safe_text(row.get(title_col, ""))
            if not title:
                continue
            summary = _safe_text(row.get(summary_col, "")) if summary_col else ""
            source = _safe_text(row.get(source_col, "")) if source_col else "东方财富"
            published_at = _normalize_published_at(row.get(published_col, "")) if published_col else datetime.now(SHANGHAI_TZ)
            url = _safe_text(row.get(url_col, "")) if url_col else ""
            if not url:
                url = _build_fallback_url(code, title, published_at.isoformat(timespec="seconds"))

            sentiment_score = score_news_text(title, summary)
            sentiment_label = _sentiment_label_from_score(sentiment_score)
            items.append(
                StockNewsItem(
                    symbol=code,
                    title=title,
                    summary=summary,
                    published_at=published_at.isoformat(timespec="seconds"),
                    source=source or "东方财富",
                    url=url,
                    sentiment_score=sentiment_score,
                    sentiment_label=sentiment_label,
                )
            )

        items.sort(key=lambda item: item.published_at, reverse=True)
        return items


class StockNewsSyncService:
    def __init__(self, store: StockNewsStore, providers: list[StockNewsProvider]):
        self.store = store
        self.providers = list(providers)

    def sync_symbol(self, symbol: str, limit: int = 50) -> StockNewsSyncResult:
        normalized = normalize_symbol(symbol)
        errors: list[str] = []
        for provider in self.providers:
            try:
                items = provider.fetch_latest(normalized, limit=limit)
            except ProviderError as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if not items:
                continue
            rows = self.store.upsert_many(items)
            return StockNewsSyncResult(symbol=normalized, provider=provider.name, rows=rows)
        if errors:
            raise ProviderError("; ".join(errors))
        return StockNewsSyncResult(symbol=normalized, provider="none", rows=0)


def build_default_news_providers() -> list[StockNewsProvider]:
    return [AkShareStockNewsProvider()]


def build_news_sentiment_analysis(items: list[StockNewsItem]) -> dict[str, object]:
    if not items:
        return {
            "available": False,
            "signal": "neutral",
            "signalLabel": _NEWS_SIGNAL_LABELS["neutral"],
            "score": 50,
            "summary": "最近消息面暂无可用样本，综合判断将以技术层和资金层为主。",
            "lookbackDays": DEFAULT_NEWS_LOOKBACK_DAYS,
            "headlines": [],
            "positiveCount": 0,
            "negativeCount": 0,
            "neutralCount": 0,
            "updatedAt": None,
        }

    now = datetime.now(SHANGHAI_TZ)
    weighted = 0.0
    weight_total = 0.0
    positive_count = 0
    negative_count = 0
    neutral_count = 0

    for item in items:
        try:
            published = datetime.fromisoformat(item.published_at)
        except ValueError:
            published = now
        if published.tzinfo is None:
            published = published.replace(tzinfo=SHANGHAI_TZ)
        age_days = max(0.0, (now - published).total_seconds() / 86400)
        recency_weight = math.exp(-age_days / 2.5)
        weight = max(0.2, recency_weight)
        weighted += float(item.sentiment_score) * weight
        weight_total += weight

        if item.sentiment_score > 0.15:
            positive_count += 1
        elif item.sentiment_score < -0.15:
            negative_count += 1
        else:
            neutral_count += 1

    blended_score = weighted / weight_total if weight_total > 0 else 0.0
    if blended_score >= 0.2:
        signal = "bullish"
    elif blended_score <= -0.2:
        signal = "bearish"
    else:
        signal = "neutral"

    normalized_score = int(max(0, min(100, round((blended_score + 1.0) * 50))))
    headlines = [item.to_dict() for item in items[:5]]
    latest = items[0]

    summary = (
        f"最近消息面样本 {len(items)} 条，偏多 {positive_count} 条、偏空 {negative_count} 条、中性 {neutral_count} 条，"
        f"综合情绪 {_NEWS_SIGNAL_LABELS[signal]}（{normalized_score}/100）。"
    )

    return {
        "available": True,
        "signal": signal,
        "signalLabel": _NEWS_SIGNAL_LABELS[signal],
        "score": normalized_score,
        "summary": summary,
        "lookbackDays": DEFAULT_NEWS_LOOKBACK_DAYS,
        "headlines": headlines,
        "positiveCount": positive_count,
        "negativeCount": negative_count,
        "neutralCount": neutral_count,
        "updatedAt": latest.updated_at,
    }


def score_news_text(title: str, summary: str = "") -> float:
    text = f"{title} {summary}".lower()
    positive_hits = sum(1 for term in _POSITIVE_TERMS if term in text)
    negative_hits = sum(1 for term in _NEGATIVE_TERMS if term in text)

    if positive_hits == 0 and negative_hits == 0:
        return 0.0

    raw = (positive_hits - negative_hits) / max(1, positive_hits + negative_hits)
    return max(-1.0, min(1.0, raw))


def _sentiment_label_from_score(score: float) -> str:
    if score >= 0.2:
        return _NEWS_SIGNAL_LABELS["bullish"]
    if score <= -0.2:
        return _NEWS_SIGNAL_LABELS["bearish"]
    return _NEWS_SIGNAL_LABELS["neutral"]


def _find_column(columns, candidates: tuple[str, ...]) -> str | None:
    table = {str(column).strip().lower(): str(column) for column in columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in table:
            return table[key]
    return None


def _safe_text(value: object) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"nan", "none"} else text


def _normalize_published_at(value: object) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI_TZ)
    text = _safe_text(value)
    if not text:
        return datetime.now(SHANGHAI_TZ)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = pd.to_datetime(text, errors="coerce").to_pydatetime() if pd.notna(pd.to_datetime(text, errors="coerce")) else datetime.now(SHANGHAI_TZ)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=SHANGHAI_TZ)
    return moment.astimezone(SHANGHAI_TZ)


def _build_fallback_url(symbol: str, title: str, published_at: str) -> str:
    digest = hashlib.sha1(f"{symbol}|{title}|{published_at}".encode("utf-8")).hexdigest()[:16]
    return f"akshare://news/{symbol}/{digest}"

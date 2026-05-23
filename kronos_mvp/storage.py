from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from collections.abc import Iterator
from pathlib import Path

from .models import Candle


class CandleStore:
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
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    amount REAL,
                    PRIMARY KEY (symbol, date)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_candles_symbol_date ON candles(symbol, date)")

    def upsert_many(self, symbol: str, candles: list[Candle]) -> int:
        normalized = normalize_symbol(symbol)
        rows = [
            (
                normalized,
                candle.date.isoformat(),
                candle.open,
                candle.high,
                candle.low,
                candle.close,
                candle.volume,
                candle.amount,
            )
            for candle in candles
        ]
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO candles(symbol, date, open, high, low, close, volume, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    amount = excluded.amount
                """,
                rows,
            )
        return len(rows)

    def get_latest(self, symbol: str, limit: int = 512) -> list[Candle]:
        if limit <= 0:
            return []
        normalized = normalize_symbol(symbol)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT date, open, high, low, close, volume, amount
                FROM candles
                WHERE symbol = ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (normalized, limit),
            ).fetchall()
        candles = [_row_to_candle(row) for row in rows]
        candles.reverse()
        return candles

    def get_latest_date(self, symbol: str) -> date | None:
        normalized = normalize_symbol(symbol)
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT MAX(date) AS latest_date
                FROM candles
                WHERE symbol = ?
                """,
                (normalized,),
            ).fetchone()
        if row is None or row["latest_date"] is None:
            return None
        return date.fromisoformat(row["latest_date"])


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().lower().replace("sh.", "").replace("sz.", "").replace("bj.", "")


def _row_to_candle(row: sqlite3.Row) -> Candle:
    return Candle(
        date=date.fromisoformat(row["date"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        amount=None if row["amount"] is None else float(row["amount"]),
    )

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Candle


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_SYNC_METADATA_KEY = "last_updated_at"


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
                    turnover REAL,
                    PRIMARY KEY (symbol, date)
                )
                """
            )
            self._ensure_column(conn, "candles", "turnover", "REAL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_candles_symbol_date ON candles(symbol, date)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def upsert_many(self, symbol: str, candles: list[Candle]) -> int:
        if not candles:
            return 0

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
                candle.turnover,
            )
            for candle in candles
        ]
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO candles(symbol, date, open, high, low, close, volume, amount, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    amount = excluded.amount,
                    turnover = excluded.turnover
                """,
                rows,
            )
            self._touch_last_updated_at(conn)
        return len(rows)

    def get_latest(self, symbol: str, limit: int = 512) -> list[Candle]:
        if limit <= 0:
            return []
        normalized = normalize_symbol(symbol)
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT date, open, high, low, close, volume, amount, turnover
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

    def get_earliest_date(self, symbol: str) -> date | None:
        normalized = normalize_symbol(symbol)
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT MIN(date) AS earliest_date
                FROM candles
                WHERE symbol = ?
                """,
                (normalized,),
            ).fetchone()
        if row is None or row["earliest_date"] is None:
            return None
        return date.fromisoformat(row["earliest_date"])

    def replace_symbol_history(self, symbol: str, candles: list[Candle]) -> int:
        if not candles:
            return 0

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
                candle.turnover,
            )
            for candle in candles
        ]
        with self._connection() as conn:
            conn.execute("DELETE FROM candles WHERE symbol = ?", (normalized,))
            conn.executemany(
                """
                INSERT INTO candles(symbol, date, open, high, low, close, volume, amount, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._touch_last_updated_at(conn)
        return len(rows)

    def replace_symbol_history_from_date(self, symbol: str, candles: list[Candle], from_date: date) -> int:
        if not candles:
            return 0

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
                candle.turnover,
            )
            for candle in candles
        ]
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM candles WHERE symbol = ? AND date >= ?",
                (normalized, from_date.isoformat()),
            )
            conn.executemany(
                """
                INSERT INTO candles(symbol, date, open, high, low, close, volume, amount, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._touch_last_updated_at(conn)
        return len(rows)

    def get_last_updated_at(self) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM sync_metadata WHERE key = ?",
                (_SYNC_METADATA_KEY,),
            ).fetchone()
            has_rows = conn.execute("SELECT 1 FROM candles LIMIT 1").fetchone() is not None
        if row is not None and row["value"] is not None:
            return str(row["value"])
        if has_rows and self.db_path.exists() and self.db_path.stat().st_size > 0:
            return datetime.fromtimestamp(self.db_path.stat().st_mtime, tz=SHANGHAI_TZ).isoformat(timespec="seconds")
        return None

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
                    WHERE type = 'table' AND name = 'candles'
                    """
                ).fetchone()
                if row is None:
                    return 0

                total = conn.execute("SELECT COUNT(*) AS total FROM source_db.candles").fetchone()["total"]
                source_turnover = "turnover" if _table_has_column(conn, "source_db", "candles", "turnover") else "NULL"
                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO candles(symbol, date, open, high, low, close, volume, amount, turnover)
                    SELECT symbol, date, open, high, low, close, volume, amount, {source_turnover}
                    FROM source_db.candles
                    """
                )
                self._touch_last_updated_at(conn)
                conn.commit()
            finally:
                conn.execute("DETACH DATABASE source_db")
        return int(total)

    def _touch_last_updated_at(self, conn: sqlite3.Connection, value: str | None = None) -> None:
        timestamp = value or datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO sync_metadata(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_SYNC_METADATA_KEY, timestamp),
        )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
        turnover=None if row["turnover"] is None else float(row["turnover"]),
    )


def _table_has_column(conn: sqlite3.Connection, schema: str, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return any(str(row[1]) == column for row in rows)

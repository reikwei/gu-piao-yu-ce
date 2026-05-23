from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .models import Candle
from .storage import normalize_symbol


class ProviderError(RuntimeError):
    pass


class DataProvider(Protocol):
    name: str

    def fetch_daily(self, symbol: str) -> list[Candle]:
        raise NotImplementedError


@dataclass
class MemoryProvider:
    name: str
    candles: list[Candle] | None = None
    error: Exception | None = None

    def fetch_daily(self, symbol: str) -> list[Candle]:
        if self.error is not None:
            raise self.error
        return list(self.candles or [])


class AkShareDailyProvider:
    name = "akshare"

    def fetch_daily(self, symbol: str) -> list[Candle]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("akshare is not installed") from exc

        code = normalize_symbol(symbol)
        try:
            frame = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
        except Exception as exc:
            raise ProviderError(str(exc)) from exc
        if frame is None or frame.empty:
            raise ProviderError("akshare returned no rows")

        candles: list[Candle] = []
        for _, row in frame.iterrows():
            try:
                candles.append(
                    Candle(
                        date=_parse_date(row["日期"]),
                        open=float(row["开盘"]),
                        high=float(row["最高"]),
                        low=float(row["最低"]),
                        close=float(row["收盘"]),
                        volume=float(row["成交量"]),
                        amount=float(row["成交额"]) if "成交额" in row and row["成交额"] is not None else None,
                    )
                )
            except KeyError as exc:
                raise ProviderError(f"akshare schema changed, missing {exc}") from exc
        return candles


class BaoStockDailyProvider:
    name = "baostock"

    def fetch_daily(self, symbol: str) -> list[Candle]:
        try:
            import baostock as bs
        except ImportError as exc:
            raise ProviderError("baostock is not installed") from exc

        code = _baostock_symbol(symbol)
        login = bs.login()
        if login.error_code != "0":
            raise ProviderError(f"baostock login failed: {login.error_msg}")
        try:
            result = bs.query_history_k_data_plus(
                code,
                "date,open,high,low,close,volume,amount",
                frequency="d",
                adjustflag="2",
            )
            if result.error_code != "0":
                raise ProviderError(result.error_msg)
            rows = []
            while result.next():
                rows.append(result.get_row_data())
        finally:
            bs.logout()

        if not rows:
            raise ProviderError("baostock returned no rows")
        return [
            Candle(
                date=_parse_date(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5] or 0),
                amount=float(row[6] or 0),
            )
            for row in rows
        ]


class TuShareDailyProvider:
    name = "tushare"

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("TUSHARE_TOKEN")

    def fetch_daily(self, symbol: str) -> list[Candle]:
        if not self.token:
            raise ProviderError("TUSHARE_TOKEN is not set")
        try:
            import tushare as ts
        except ImportError as exc:
            raise ProviderError("tushare is not installed") from exc

        pro = ts.pro_api(self.token)
        code = _tushare_symbol(symbol)
        try:
            frame = pro.daily(ts_code=code)
        except Exception as exc:
            raise ProviderError(str(exc)) from exc
        if frame is None or frame.empty:
            raise ProviderError("tushare returned no rows")

        frame = frame.sort_values("trade_date")
        return [
            Candle(
                date=_parse_date(row["trade_date"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["vol"]),
                amount=float(row["amount"]),
            )
            for _, row in frame.iterrows()
        ]


def build_default_providers() -> list[DataProvider]:
    names = [name.strip().lower() for name in os.getenv("DATA_PROVIDERS", "akshare,baostock,tushare").split(",")]
    providers: list[DataProvider] = []
    for name in names:
        if name == "akshare":
            providers.append(AkShareDailyProvider())
        elif name == "baostock":
            providers.append(BaoStockDailyProvider())
        elif name == "tushare":
            providers.append(TuShareDailyProvider())
    return providers


def _parse_date(value: object) -> date:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10])


def _baostock_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    prefix = "sh" if code.startswith("6") else "sz"
    return f"{prefix}.{code}"


def _tushare_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    suffix = "SH" if code.startswith("6") else "SZ"
    return f"{code}.{suffix}"

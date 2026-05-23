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

    def fetch_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        raise NotImplementedError


@dataclass
class MemoryProvider:
    name: str
    candles: list[Candle] | None = None
    error: Exception | None = None

    def fetch_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        if self.error is not None:
            raise self.error
        candles = list(self.candles or [])
        if start_date is not None:
            candles = [candle for candle in candles if candle.date >= start_date]
        return candles


class AkShareDailyProvider:
    name = "akshare"

    def fetch_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("akshare is not installed") from exc

        code = normalize_symbol(symbol)
        try:
            frame = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=_format_compact_date(start_date),
                end_date="",
                adjust="qfq",
            )
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

    def fetch_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
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
                start_date=_format_iso_date(start_date),
                end_date="",
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

    def fetch_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        if not self.token:
            raise ProviderError("TUSHARE_TOKEN is not set")
        try:
            import tushare as ts
        except ImportError as exc:
            raise ProviderError("tushare is not installed") from exc

        pro = ts.pro_api(self.token)
        code = _tushare_symbol(symbol)
        try:
            frame = pro.daily(ts_code=code, start_date=_format_compact_date(start_date), end_date="")
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
    names = _configured_provider_names()
    providers: list[DataProvider] = []
    for name in names:
        if name == "akshare":
            providers.append(AkShareDailyProvider())
        elif name == "baostock":
            providers.append(BaoStockDailyProvider())
        elif name == "tushare":
            providers.append(TuShareDailyProvider())
    return providers


def list_a_share_symbols(market: str = "all") -> list[str]:
    errors: list[str] = []
    for name in _configured_provider_names():
        try:
            if name == "akshare":
                symbols = _list_symbols_from_akshare()
            elif name == "baostock":
                symbols = _list_symbols_from_baostock()
            elif name == "tushare":
                symbols = _list_symbols_from_tushare(os.getenv("TUSHARE_TOKEN"))
            else:
                continue
            normalized = sorted(
                {
                    normalize_symbol(symbol)
                    for symbol in symbols
                    if _is_a_share_symbol(normalize_symbol(symbol))
                    and (market == "all" or infer_a_share_market(normalize_symbol(symbol)) == market)
                }
            )
            if normalized:
                return normalized
            raise ProviderError("returned no A-share symbols")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise ProviderError("; ".join(errors) if errors else "no providers configured")


def _parse_date(value: object) -> date:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10])


def _configured_provider_names() -> list[str]:
    return [name.strip().lower() for name in os.getenv("DATA_PROVIDERS", "akshare,baostock,tushare").split(",") if name.strip()]


def _format_iso_date(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def _format_compact_date(value: date | None) -> str:
    return value.strftime("%Y%m%d") if value is not None else ""


def _is_a_share_symbol(symbol: str) -> bool:
    code = normalize_symbol(symbol)
    if len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(("000", "001", "002", "003", "300", "301", "430", "600", "601", "603", "605", "688", "689", "8", "92"))


def infer_a_share_market(symbol: str) -> str:
    code = normalize_symbol(symbol)
    if code.startswith(("43", "8", "92")):
        return "bj"
    if code.startswith("6"):
        return "sh"
    return "sz"


def _list_symbols_from_akshare() -> list[str]:
    try:
        import akshare as ak
    except ImportError as exc:
        raise ProviderError("akshare is not installed") from exc

    try:
        frame = ak.stock_info_a_code_name()
    except Exception as exc:
        raise ProviderError(str(exc)) from exc
    if frame is None or frame.empty:
        raise ProviderError("akshare returned no symbols")
    for column in ("code", "代码", "A股代码", "证券代码"):
        if column in frame.columns:
            return frame[column].astype(str).tolist()
    raise ProviderError("akshare symbol schema changed")


def _list_symbols_from_baostock() -> list[str]:
    try:
        import baostock as bs
    except ImportError as exc:
        raise ProviderError("baostock is not installed") from exc

    login = bs.login()
    if login.error_code != "0":
        raise ProviderError(f"baostock login failed: {login.error_msg}")
    try:
        result = bs.query_all_stock()
        if result.error_code != "0":
            raise ProviderError(result.error_msg)
        rows: list[str] = []
        while result.next():
            row = result.get_row_data()
            if row:
                rows.append(row[0])
    finally:
        bs.logout()
    if not rows:
        raise ProviderError("baostock returned no symbols")
    return rows


def _list_symbols_from_tushare(token: str | None) -> list[str]:
    if not token:
        raise ProviderError("TUSHARE_TOKEN is not set")
    try:
        import tushare as ts
    except ImportError as exc:
        raise ProviderError("tushare is not installed") from exc

    pro = ts.pro_api(token)
    try:
        frame = pro.stock_basic(exchange="", list_status="L", fields="ts_code")
    except Exception as exc:
        raise ProviderError(str(exc)) from exc
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        raise ProviderError("tushare returned no symbols")
    return frame["ts_code"].astype(str).tolist()


def _baostock_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    prefix = "sh"
    if code.startswith(("43", "8", "92")):
        prefix = "bj"
    elif not code.startswith("6"):
        prefix = "sz"
    return f"{prefix}.{code}"


def _tushare_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    suffix = "SH"
    if code.startswith(("43", "8", "92")):
        suffix = "BJ"
    elif not code.startswith("6"):
        suffix = "SZ"
    return f"{code}.{suffix}"

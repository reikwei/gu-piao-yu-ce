from __future__ import annotations

import atexit
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Protocol
from zoneinfo import ZoneInfo

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
        market = infer_a_share_market(code)
        errors: list[str] = []
        if market == "bj":
            try:
                return _fetch_bj_daily_from_akshare_sina(ak, code, start_date)
            except ProviderError as exc:
                errors.append(f"sina: {exc}")

        frame = None
        for adjust in _akshare_hist_adjusts(code):
            try:
                frame = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=_format_compact_date(start_date),
                    end_date="20500101",
                    adjust=adjust,
                )
            except Exception as exc:
                prefix = "eastmoney " if market == "bj" else ""
                errors.append(f"{prefix}adjust={adjust or 'none'}: {exc}")
                continue
            if frame is not None and not frame.empty:
                break
            prefix = "eastmoney " if market == "bj" else ""
            errors.append(f"{prefix}adjust={adjust or 'none'}: returned no rows")

        if frame is None or frame.empty:
            if errors:
                raise ProviderError("; ".join(errors))
            raise ProviderError("akshare returned no rows")

        return _build_candles_from_akshare_hist(frame)


class BaoStockDailyProvider:
    name = "baostock"

    def __init__(self) -> None:
        self._client = None
        self._logged_in = False
        self._logout_registered = False

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import baostock as bs
        except ImportError as exc:
            raise ProviderError("baostock is not installed") from exc
        self._client = bs
        return self._client

    def _ensure_login(self):
        bs = self._get_client()
        if self._logged_in:
            return bs
        login = bs.login()
        if login.error_code != "0":
            raise ProviderError(f"baostock login failed: {login.error_msg}")
        self._logged_in = True
        if not self._logout_registered:
            atexit.register(self._logout)
            self._logout_registered = True
        return bs

    def _logout(self) -> None:
        if not self._logged_in or self._client is None:
            return
        try:
            self._client.logout()
        except Exception:
            pass
        finally:
            self._logged_in = False

    def fetch_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        if infer_a_share_market(symbol) == "bj":
            raise ProviderError("baostock does not support BJ A-share history")

        code = _baostock_symbol(symbol)
        bs = self._ensure_login()
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
                symbols = _list_symbols_from_akshare(market=market)
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


def _akshare_hist_adjusts(symbol: str) -> tuple[str, ...]:
    if infer_a_share_market(symbol) == "bj":
        return ("", "qfq", "hfq")
    return ("qfq",)


def _fetch_bj_daily_from_akshare_sina(ak: object, symbol: str, start_date: date | None) -> list[Candle]:
    try:
        frame = ak.stock_zh_a_daily(
            symbol=_akshare_sina_symbol(symbol),
            adjust="",
        )
    except Exception as exc:
        raise ProviderError(str(exc)) from exc
    if frame is None or frame.empty:
        raise ProviderError("returned no rows")
    frame = frame.copy()
    frame["date"] = frame["date"].map(_parse_date)
    if start_date is not None:
        frame = frame[frame["date"] >= start_date]
    if frame.empty:
        raise ProviderError("returned no rows")
    return _build_candles_from_akshare_sina(frame)


def _build_candles_from_akshare_hist(frame) -> list[Candle]:
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


def _build_candles_from_akshare_sina(frame) -> list[Candle]:
    candles: list[Candle] = []
    for _, row in frame.iterrows():
        try:
            candles.append(
                Candle(
                    date=_parse_date(row["date"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    amount=float(row["amount"]) if "amount" in row and row["amount"] is not None else None,
                )
            )
        except KeyError as exc:
            raise ProviderError(f"akshare sina schema changed, missing {exc}") from exc
    return candles


def _akshare_sina_symbol(symbol: str) -> str:
    code = normalize_symbol(symbol)
    market = infer_a_share_market(code)
    if market == "bj":
        prefix = "bj"
    elif market == "sh":
        prefix = "sh"
    else:
        prefix = "sz"
    return f"{prefix}{code}"


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


def _list_symbols_from_akshare(market: str = "all") -> list[str]:
    try:
        import akshare as ak
    except ImportError as exc:
        raise ProviderError("akshare is not installed") from exc

    errors: list[str] = []
    for loader in _akshare_symbol_loaders(ak, market):
        try:
            frame = _call_with_retries(loader, attempts=3)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if frame is None or frame.empty:
            errors.append("akshare returned no symbols")
            continue

        for column in ("code", "代码", "A股代码", "证券代码"):
            if column in frame.columns:
                return frame[column].astype(str).tolist()
        errors.append("akshare symbol schema changed")

    raise ProviderError("; ".join(errors) if errors else "akshare returned no symbols")


def _akshare_symbol_loaders(ak: object, market: str):
    loader_names = {
        "all": ["stock_info_a_code_name"],
        "sh": ["stock_info_sh_name_code", "stock_info_a_code_name"],
        "sz": ["stock_info_sz_name_code", "stock_info_a_code_name"],
        "bj": ["stock_info_bj_name_code", "stock_info_a_code_name"],
    }.get(market, ["stock_info_a_code_name"])

    loaders = []
    for name in loader_names:
        loader = getattr(ak, name, None)
        if callable(loader):
            loaders.append(loader)
    if not loaders:
        raise ProviderError("akshare does not expose a stock symbol loader for this market")
    return loaders


def _call_with_retries(loader, attempts: int = 3):
    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return loader()
        except Exception as exc:  # pragma: no cover - exercised via provider tests with injected failures
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    raise ProviderError("loader did not execute")


def _list_symbols_from_baostock() -> list[str]:
    try:
        import baostock as bs
    except ImportError as exc:
        raise ProviderError("baostock is not installed") from exc

    login = bs.login()
    if login.error_code != "0":
        raise ProviderError(f"baostock login failed: {login.error_msg}")
    try:
        result = bs.query_all_stock(day=_latest_a_share_trading_day().isoformat())
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


@lru_cache(maxsize=1)
def _get_a_share_calendar():
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange_calendars is required for A-share symbol discovery. "
            "Install project requirements before syncing all symbols."
        ) from exc
    return xcals.get_calendar("XSHG")


def _latest_a_share_trading_day() -> date:
    import pandas as pd

    shanghai_today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    calendar = _get_a_share_calendar()
    start = pd.Timestamp(shanghai_today - timedelta(days=14))
    end = pd.Timestamp(shanghai_today)
    sessions = calendar.sessions_in_range(start, end)
    if len(sessions) == 0:
        raise ProviderError("unable to resolve latest A-share trading day")
    return sessions[-1].date()

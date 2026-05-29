from __future__ import annotations

import atexit
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Protocol
from zoneinfo import ZoneInfo

from .models import Candle
from .instruments import is_market_index_symbol, market_index_info
from .storage import normalize_symbol


class ProviderError(RuntimeError):
    pass


class DataProvider(Protocol):
    name: str

    def fetch_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        raise NotImplementedError


@dataclass(frozen=True)
class RelativeIndustryMapping:
    symbol: str
    industry_name: str


class RelativeStrengthProvider(Protocol):
    name: str

    def fetch_index_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        raise NotImplementedError

    def fetch_industry_mappings(self) -> list[RelativeIndustryMapping]:
        raise NotImplementedError

    def fetch_industry_mappings_for_symbols(self, symbols: list[str]) -> list[RelativeIndustryMapping]:
        raise NotImplementedError

    def fetch_industry_daily(self, industry_name: str, start_date: date | None = None) -> list[Candle]:
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

        index_info = market_index_info(symbol)
        if index_info is not None:
            return _fetch_akshare_index_daily(ak, index_info.provider_symbol, start_date)

        code = normalize_symbol(symbol)
        market = infer_a_share_market(code)
        errors: list[str] = []
        if _is_akshare_cdr_symbol(code):
            try:
                return _fetch_akshare_cdr_daily(ak, code, start_date)
            except ProviderError as exc:
                errors.append(f"cdr: {exc}")

        if market == "bj":
            try:
                return _fetch_akshare_sina_daily(ak, code, start_date, adjust="")
            except ProviderError as exc:
                errors.append(f"sina: {exc}")

        frame = None
        saw_hist_exception = False
        for adjust in _akshare_hist_adjusts(code):
            try:
                frame = _call_with_retries(
                    lambda adjust=adjust: ak.stock_zh_a_hist(
                        symbol=code,
                        period="daily",
                        start_date=_format_compact_date(start_date),
                        end_date="20500101",
                        adjust=adjust,
                    ),
                    attempts=3,
                )
            except Exception as exc:
                saw_hist_exception = True
                prefix = "eastmoney " if market == "bj" else ""
                errors.append(f"{prefix}adjust={adjust or 'none'}: {exc}")
                continue
            if frame is not None and not frame.empty:
                break
            prefix = "eastmoney " if market == "bj" else ""
            errors.append(f"{prefix}adjust={adjust or 'none'}: returned no rows")

        if frame is None or frame.empty:
            if market != "bj" and saw_hist_exception:
                try:
                    return _fetch_akshare_sina_daily(ak, code, start_date, adjust="qfq")
                except ProviderError as exc:
                    errors.append(f"sina adjust=qfq: {exc}")
            if errors:
                raise ProviderError("; ".join(errors))
            raise ProviderError("akshare returned no rows")

        return _build_candles_from_akshare_hist(frame)


class AkShareRelativeStrengthProvider:
    name = "akshare"

    def fetch_index_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("akshare is not installed") from exc

        try:
            frame = _call_with_retries(
                lambda: ak.stock_zh_index_daily_em(
                    symbol=symbol,
                    start_date=_format_compact_date(start_date) or "19900101",
                    end_date="20500101",
                ),
                attempts=3,
            )
        except Exception as exc:
            raise ProviderError(str(exc)) from exc

        if frame is None or frame.empty:
            if start_date is not None:
                return []
            raise ProviderError("akshare returned no index rows")
        return _build_candles_from_akshare_ohlc(frame, start_date=start_date)

    def fetch_industry_mappings(self) -> list[RelativeIndustryMapping]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("akshare is not installed") from exc

        try:
            frame = _call_with_retries(ak.stock_board_industry_name_em, attempts=3)
        except Exception as exc:
            raise ProviderError(str(exc)) from exc

        if frame is None or frame.empty:
            raise ProviderError("akshare returned no industries")

        name_column = _find_frame_column(frame.columns, ("板块名称", "行业名称", "名称", "板块"))
        if name_column is None:
            raise ProviderError("akshare industry schema changed, missing name column")

        mappings: list[RelativeIndustryMapping] = []
        seen_symbols: set[str] = set()
        errors: list[str] = []
        for _, row in frame.iterrows():
            industry_name = str(row[name_column]).strip()
            if not industry_name or industry_name.lower() in {"nan", "none"}:
                continue
            try:
                members = _call_with_retries(
                    lambda industry_name=industry_name: ak.stock_board_industry_cons_em(symbol=industry_name),
                    attempts=3,
                )
            except Exception as exc:
                errors.append(f"{industry_name}: {exc}")
                continue
            if members is None or members.empty:
                continue

            code_column = _find_frame_column(members.columns, ("代码", "股票代码", "证券代码", "A股代码", "code"))
            if code_column is None:
                errors.append(f"{industry_name}: missing code column")
                continue

            for _, member in members.iterrows():
                code = _normalize_a_share_code_text(member[code_column])
                if not _is_a_share_symbol(code) or code in seen_symbols:
                    continue
                seen_symbols.add(code)
                mappings.append(RelativeIndustryMapping(symbol=code, industry_name=industry_name))

        if mappings:
            return mappings
        raise ProviderError("; ".join(errors) if errors else "akshare returned no industry mappings")

    def fetch_industry_mappings_for_symbols(self, symbols: list[str]) -> list[RelativeIndustryMapping]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("akshare is not installed") from exc

        requested: list[str] = []
        seen_symbols: set[str] = set()
        for symbol in symbols:
            code = normalize_symbol(symbol)
            if not _is_a_share_symbol(code) or code in seen_symbols:
                continue
            seen_symbols.add(code)
            requested.append(code)

        mappings: list[RelativeIndustryMapping] = []
        errors: list[str] = []
        for symbol in requested:
            try:
                frame = _call_with_retries(
                    lambda symbol=symbol: ak.stock_individual_info_em(symbol=symbol),
                    attempts=3,
                )
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
                continue

            industry_name = _extract_em_industry_name(frame)
            if not industry_name:
                errors.append(f"{symbol}: missing industry")
                continue
            mappings.append(RelativeIndustryMapping(symbol=symbol, industry_name=industry_name))

        if mappings:
            return mappings
        raise ProviderError("; ".join(errors) if errors else "akshare returned no requested industry mappings")

    def fetch_industry_daily(self, industry_name: str, start_date: date | None = None) -> list[Candle]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise ProviderError("akshare is not installed") from exc

        try:
            frame = _call_with_retries(
                lambda: ak.stock_board_industry_hist_em(
                    symbol=industry_name,
                    start_date=_format_compact_date(start_date) or "19900101",
                    end_date="20500101",
                    period="日k",
                    adjust="",
                ),
                attempts=3,
            )
        except Exception as exc:
            raise ProviderError(str(exc)) from exc

        if frame is None or frame.empty:
            if start_date is not None:
                return []
            raise ProviderError("akshare returned no industry rows")
        return _build_candles_from_akshare_ohlc(frame, start_date=start_date)


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
        if is_market_index_symbol(symbol):
            raise ProviderError("baostock does not support market index history")
        if infer_a_share_market(symbol) == "bj":
            raise ProviderError("baostock does not support BJ A-share history")

        code = _baostock_symbol(symbol)
        bs = self._ensure_login()
        result = bs.query_history_k_data_plus(
            code,
            "date,open,high,low,close,volume,amount,turn",
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
            if start_date is not None:
                return []
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
                turnover=_normalize_turnover_rate(row[7], source="baostock") if len(row) > 7 else None,
            )
            for row in rows
        ]


class TuShareDailyProvider:
    name = "tushare"

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("TUSHARE_TOKEN")

    def fetch_daily(self, symbol: str, start_date: date | None = None) -> list[Candle]:
        if is_market_index_symbol(symbol):
            raise ProviderError("tushare daily provider does not support market index history")
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
                turnover=None,
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


def lookup_a_share_name(symbol: str) -> str | None:
    index_info = market_index_info(symbol)
    if index_info is not None:
        return index_info.name

    normalized = normalize_symbol(symbol)
    if not _is_a_share_symbol(normalized):
        return None
    try:
        return _list_symbol_name_map_from_akshare(infer_a_share_market(normalized)).get(normalized)
    except ProviderError:
        return None


def _parse_date(value: object) -> date:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text[:10])


def _configured_provider_names() -> list[str]:
    return [name.strip().lower() for name in os.getenv("DATA_PROVIDERS", "akshare,baostock,tushare").split(",") if name.strip()]


@lru_cache(maxsize=4)
def _list_symbol_name_map_from_akshare(market: str) -> dict[str, str]:
    try:
        import akshare as ak
    except ImportError as exc:
        raise ProviderError("akshare is not installed") from exc

    mapping: dict[str, str] = {}
    errors: list[str] = []
    for loader in _akshare_symbol_loaders(ak, market):
        try:
            frame = _call_with_retries(loader, attempts=3)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if frame is None or frame.empty:
            continue

        code_column = _find_frame_column(frame.columns, ("code", "代码", "A股代码", "证券代码", "股票代码"))
        name_column = _find_frame_column(frame.columns, ("name", "名称", "证券简称", "股票简称", "公司简称", "简称"))
        if code_column is None or name_column is None:
            errors.append("akshare symbol schema changed")
            continue

        for _, row in frame.iterrows():
            code = normalize_symbol(str(row[code_column]))
            name = str(row[name_column]).strip()
            if not _is_a_share_symbol(code):
                continue
            if market != "all" and infer_a_share_market(code) != market:
                continue
            if not name or name.lower() in {"nan", "none"}:
                continue
            mapping[code] = name

    if mapping:
        return mapping
    raise ProviderError("; ".join(errors) if errors else "akshare returned no symbol names")


def _find_frame_column(columns, candidates: tuple[str, ...]) -> str | None:
    available = {str(column).strip().lower(): str(column) for column in columns}
    for candidate in candidates:
        match = available.get(candidate.strip().lower())
        if match is not None:
            return match
    return None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    return float(text)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    return text


def _normalize_turnover_rate(value: object, source: str) -> float | None:
    numeric = _optional_float(value)
    if numeric is None:
        return None
    # AkShare 当前实盘环境返回的是 0.0177 这类分数，统一转成 1.77% 口径入库。
    if source == "akshare" and abs(numeric) <= 1:
        return numeric * 100
    return numeric


def _format_iso_date(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def _format_compact_date(value: date | None) -> str:
    return value.strftime("%Y%m%d") if value is not None else ""


def _akshare_hist_adjusts(symbol: str) -> tuple[str, ...]:
    if infer_a_share_market(symbol) == "bj":
        return ("", "qfq", "hfq")
    return ("qfq",)


def _fetch_akshare_sina_daily(ak: object, symbol: str, start_date: date | None, adjust: str) -> list[Candle]:
    try:
        frame = _call_with_retries(
            lambda: ak.stock_zh_a_daily(
                symbol=_akshare_sina_symbol(symbol),
                adjust=adjust,
            ),
            attempts=3,
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
        if start_date is not None:
            return []
        raise ProviderError("returned no rows")
    return _build_candles_from_akshare_sina(frame)


def _fetch_akshare_cdr_daily(ak: object, symbol: str, start_date: date | None) -> list[Candle]:
    try:
        frame = _call_with_retries(
            lambda: ak.stock_zh_a_cdr_daily(
                symbol=_akshare_sina_symbol(symbol),
                start_date=_format_compact_date(start_date) or "19900101",
                end_date="20500101",
            ),
            attempts=3,
        )
    except Exception as exc:
        raise ProviderError(str(exc)) from exc
    if frame is None or frame.empty:
        if start_date is not None:
            return []
        raise ProviderError("returned no rows")
    frame = frame.copy()
    frame["date"] = frame["date"].map(_parse_date)
    if start_date is not None:
        frame = frame[frame["date"] >= start_date]
    if frame.empty:
        return []
    return _build_candles_from_akshare_sina(frame)


def _fetch_akshare_index_daily(ak: object, symbol: str, start_date: date | None) -> list[Candle]:
    try:
        frame = _call_with_retries(
            lambda: ak.stock_zh_index_daily_em(
                symbol=symbol,
                start_date=_format_compact_date(start_date) or "19900101",
                end_date="20500101",
            ),
            attempts=3,
        )
    except Exception as exc:
        raise ProviderError(str(exc)) from exc
    if frame is None or frame.empty:
        if start_date is not None:
            return []
        raise ProviderError("akshare returned no index rows")
    return _build_candles_from_akshare_ohlc(frame, start_date=start_date)


def _build_candles_from_akshare_hist(frame) -> list[Candle]:
    return _build_candles_from_akshare_ohlc(frame)


def _build_candles_from_akshare_ohlc(frame, start_date: date | None = None) -> list[Candle]:
    date_column = _find_frame_column(frame.columns, ("日期", "date", "交易日期", "时间"))
    open_column = _find_frame_column(frame.columns, ("开盘", "open"))
    high_column = _find_frame_column(frame.columns, ("最高", "high"))
    low_column = _find_frame_column(frame.columns, ("最低", "low"))
    close_column = _find_frame_column(frame.columns, ("收盘", "close"))
    volume_column = _find_frame_column(frame.columns, ("成交量", "volume"))
    amount_column = _find_frame_column(frame.columns, ("成交额", "amount"))
    turnover_column = _find_frame_column(frame.columns, ("换手率", "turnover", "turn", "换手"))

    missing = [
        name
        for name, column in (
            ("date", date_column),
            ("open", open_column),
            ("high", high_column),
            ("low", low_column),
            ("close", close_column),
            ("volume", volume_column),
        )
        if column is None
    ]
    if missing:
        raise ProviderError(f"akshare schema changed, missing {', '.join(missing)}")

    normalized = frame.copy()
    normalized[date_column] = normalized[date_column].map(_parse_date)
    if start_date is not None:
        normalized = normalized[normalized[date_column] >= start_date]
    if normalized.empty:
        return []

    candles: list[Candle] = []
    for _, row in normalized.sort_values(date_column).iterrows():
        try:
            candles.append(
                Candle(
                    date=_parse_date(row[date_column]),
                    open=float(row[open_column]),
                    high=float(row[high_column]),
                    low=float(row[low_column]),
                    close=float(row[close_column]),
                    volume=float(row[volume_column]),
                    amount=_optional_float(row[amount_column]) if amount_column is not None else None,
                    turnover=_normalize_turnover_rate(row[turnover_column], source="akshare") if turnover_column is not None else None,
                )
            )
        except KeyError as exc:
            raise ProviderError(f"akshare schema changed, missing {exc}") from exc
    return candles


def _build_candles_from_akshare_sina(frame) -> list[Candle]:
    turnover_column = _find_frame_column(frame.columns, ("换手率", "turnover", "turn", "换手"))
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
                    turnover=_normalize_turnover_rate(row[turnover_column], source="akshare") if turnover_column is not None else None,
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


def _is_akshare_cdr_symbol(symbol: str) -> bool:
    return normalize_symbol(symbol).startswith("689")


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
    collected: list[str] = []
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
                collected.extend(frame[column].astype(str).tolist())
                break
        else:
            errors.append("akshare symbol schema changed")

    normalized = _normalize_symbol_candidates(collected, market=market)
    if normalized:
        return normalized

    if collected:
        raise ProviderError("returned no A-share symbols")

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


def _extract_em_industry_name(frame) -> str | None:
    if frame is None or frame.empty:
        return None

    direct_column = _find_frame_column(frame.columns, ("行业", "所属行业"))
    if direct_column is not None:
        for value in frame[direct_column].tolist():
            text = _optional_text(value)
            if text is not None:
                return text

    item_column = _find_frame_column(frame.columns, ("item", "项目", "字段", "名称"))
    value_column = _find_frame_column(frame.columns, ("value", "值", "内容", "数据"))
    if item_column is None or value_column is None:
        if len(frame.columns) < 2:
            return None
        item_column = str(frame.columns[0])
        value_column = str(frame.columns[1])

    for _, row in frame.iterrows():
        label = _optional_text(row[item_column])
        if label not in {"行业", "所属行业"}:
            continue
        return _optional_text(row[value_column])
    return None


def _call_with_retries(loader, attempts: int = 3):
    last_exc: Exception | None = None
    total_attempts = max(1, attempts)
    for attempt in range(total_attempts):
        try:
            return loader()
        except Exception as exc:  # pragma: no cover - exercised via provider tests with injected failures
            last_exc = exc
            if attempt + 1 < total_attempts:
                time.sleep(0.2 * (attempt + 1))
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


def _normalize_symbol_candidates(symbols: list[str], market: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        code = normalize_symbol(symbol)
        if not _is_a_share_symbol(code):
            continue
        if market != "all" and infer_a_share_market(code) != market:
            continue
        if code in seen:
            continue
        seen.add(code)
        normalized.append(code)
    return normalized


def _normalize_a_share_code_text(value: object) -> str:
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    if text.endswith(".0"):
        text = text[:-2]

    lowered = text.lower()
    for prefix in ("sh.", "sz.", "bj.", "sh", "sz", "bj"):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break

    code = normalize_symbol(text)
    if code.isdigit() and len(code) < 6:
        code = code.zfill(6)
    return code


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

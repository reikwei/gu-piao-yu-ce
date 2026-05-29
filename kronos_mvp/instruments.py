from __future__ import annotations

from dataclasses import dataclass

from .storage import normalize_symbol


DEFAULT_MARKET_INDEX_SYMBOL = "sh000001"


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    name: str | None
    type: str
    label: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "type": self.type,
            "label": self.label,
        }


@dataclass(frozen=True)
class MarketIndexInfo:
    symbol: str
    provider_symbol: str
    name: str
    label: str


MARKET_INDEXES: dict[str, MarketIndexInfo] = {
    "sh000001": MarketIndexInfo(
        symbol="sh000001",
        provider_symbol="sh000001",
        name="上证指数",
        label="A股上证指数",
    ),
}


_MARKET_INDEX_ALIASES = {
    "sh000001": "sh000001",
    "sh.000001": "sh000001",
    "000001.sh": "sh000001",
    "sse": "sh000001",
    "xshg": "sh000001",
    "上证": "sh000001",
    "上证指数": "sh000001",
    "上证综指": "sh000001",
    "沪指": "sh000001",
    "大盘": "sh000001",
    "a股大盘": "sh000001",
    "a股上证指数": "sh000001",
    "A股大盘": "sh000001",
    "A股上证指数": "sh000001",
}


def normalize_instrument_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return ""

    compact = raw.replace(" ", "").replace("_", "").replace("-", "")
    lowered = compact.lower()
    if compact in _MARKET_INDEX_ALIASES:
        return _MARKET_INDEX_ALIASES[compact]
    if lowered in _MARKET_INDEX_ALIASES:
        return _MARKET_INDEX_ALIASES[lowered]
    return normalize_symbol(raw)


def is_market_index_symbol(symbol: str) -> bool:
    return normalize_instrument_symbol(symbol) in MARKET_INDEXES


def market_index_info(symbol: str) -> MarketIndexInfo | None:
    return MARKET_INDEXES.get(normalize_instrument_symbol(symbol))


def instrument_info(symbol: str, name: str | None = None) -> InstrumentInfo:
    normalized = normalize_instrument_symbol(symbol)
    index_info = MARKET_INDEXES.get(normalized)
    if index_info is not None:
        return InstrumentInfo(
            symbol=index_info.symbol,
            name=index_info.name,
            type="market_index",
            label=index_info.label,
        )
    return InstrumentInfo(
        symbol=normalized,
        name=name,
        type="stock",
        label=f"{normalized} {name}".strip() if name else normalized,
    )
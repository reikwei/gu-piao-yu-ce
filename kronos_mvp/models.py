from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Candle:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None

    def to_dict(self) -> dict[str, str | float | None]:
        return {
            "date": self.date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
        }


@dataclass(frozen=True)
class SyncResult:
    symbol: str
    provider: str
    rows: int


@dataclass(frozen=True)
class PredictionPoint:
    date: date
    open: float
    high: float
    low: float
    close: float

    def to_dict(self) -> dict[str, str | float]:
        return {
            "date": self.date.isoformat(),
            "open": round(self.open, 4),
            "high": round(self.high, 4),
            "low": round(self.low, 4),
            "close": round(self.close, 4),
        }


@dataclass(frozen=True)
class ForecastPath:
    name: str
    points: list[PredictionPoint]

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "points": [point.to_dict() for point in self.points]}


@dataclass(frozen=True)
class PredictionResult:
    symbol: str
    backend: str
    paths: list[ForecastPath]

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "backend": self.backend,
            "paths": [path.to_dict() for path in self.paths],
        }

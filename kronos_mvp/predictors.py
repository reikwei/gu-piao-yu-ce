from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from .models import Candle, ForecastPath, PredictionPoint, PredictionResult
from .storage import normalize_symbol


class KronosPredictor:
    backend = "kronos"

    def __init__(
        self,
        model_name: str = "NeoQuasar/Kronos-small",
        tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base",
        device: str = "cpu",
        max_context: int = 512,
        upstream_predictor: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.tokenizer_name = tokenizer_name
        self.device = device
        self.max_context = max_context
        self._upstream_predictor = upstream_predictor

    def predict(self, symbol: str, candles: list[Candle], horizon: int = 5, paths: int = 5) -> PredictionResult:
        if len(candles) < 2:
            raise ValueError("at least two candles are required")
        horizon = _clamp(horizon, 1, 60)
        paths = _clamp(paths, 1, 20)

        import pandas as pd

        history = candles[-self.max_context :]
        df = pd.DataFrame(
            {
                "open": [candle.open for candle in history],
                "high": [candle.high for candle in history],
                "low": [candle.low for candle in history],
                "close": [candle.close for candle in history],
                "volume": [candle.volume for candle in history],
                "amount": [0.0 if candle.amount is None else candle.amount for candle in history],
            }
        )
        x_timestamp = pd.Series([pd.Timestamp(candle.date) for candle in history])
        future_days = next_trading_days(history[-1].date, horizon)
        y_timestamp = pd.Series([pd.Timestamp(day) for day in future_days])
        upstream = self._get_upstream_predictor()

        forecast_paths: list[ForecastPath] = []
        for path_index in range(paths):
            pred_df = upstream.predict(
                df=df,
                x_timestamp=x_timestamp,
                y_timestamp=y_timestamp,
                pred_len=horizon,
                T=1.0,
                top_p=0.9,
                sample_count=1,
            )
            forecast_paths.append(ForecastPath(name=f"kronos_path_{path_index + 1}", points=_frame_to_points(pred_df, future_days)))

        return PredictionResult(symbol=normalize_symbol(symbol), backend=self.backend, paths=forecast_paths)

    def _get_upstream_predictor(self) -> Any:
        if self._upstream_predictor is not None:
            return self._upstream_predictor
        try:
            from model import Kronos, KronosTokenizer
            from model import KronosPredictor as UpstreamKronosPredictor
        except ImportError as exc:
            raise RuntimeError(
                "Kronos is required. Clone the upstream project and expose it on PYTHONPATH, for example: "
                "git clone https://github.com/shiyu-coder/Kronos.git vendor/Kronos "
                "and set PYTHONPATH=vendor/Kronos"
            ) from exc

        model = Kronos.from_pretrained(self.model_name)
        tokenizer = KronosTokenizer.from_pretrained(self.tokenizer_name)
        self._upstream_predictor = UpstreamKronosPredictor(model, tokenizer, device=self.device, max_context=self.max_context)
        return self._upstream_predictor


def build_predictor(name: str = "kronos"):
    if name == "kronos":
        return KronosPredictor()
    raise ValueError(f"unknown predictor backend: {name}")


def next_trading_days(start: date, count: int) -> list[date]:
    if count <= 0:
        return []

    import pandas as pd

    calendar = _get_a_share_calendar()
    start_session = pd.Timestamp(start) + pd.Timedelta(days=1)
    span_days = max(32, count * 8)
    end_session = start_session + pd.Timedelta(days=span_days)
    sessions = calendar.sessions_in_range(start_session, end_session)

    while len(sessions) < count:
        end_session += pd.Timedelta(days=span_days)
        sessions = calendar.sessions_in_range(start_session, end_session)

    return [session.date() for session in sessions[:count]]


def _average_return(candles: list[Candle]) -> float:
    returns = _returns(candles)
    return sum(returns) / len(returns) if returns else 0.0


def _average_abs_return(candles: list[Candle]) -> float:
    returns = [abs(value) for value in _returns(candles)]
    return sum(returns) / len(returns) if returns else 0.0


def _returns(candles: list[Candle]) -> list[float]:
    values: list[float] = []
    for prev, current in zip(candles, candles[1:]):
        if prev.close > 0:
            values.append((current.close - prev.close) / prev.close)
    return values


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _frame_to_points(frame: Any, dates: list[date]) -> list[PredictionPoint]:
    points: list[PredictionPoint] = []
    for index, day in enumerate(dates):
        row = frame.iloc[index]
        points.append(
            PredictionPoint(
                date=day,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
        )
    return points


@lru_cache(maxsize=1)
def _get_a_share_calendar() -> Any:
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange_calendars is required for A-share trading-day forecasting. "
            "Install project requirements before running predictions."
        ) from exc
    return xcals.get_calendar("XSHG")

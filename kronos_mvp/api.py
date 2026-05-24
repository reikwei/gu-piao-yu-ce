from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from dotenv import load_dotenv

from .models import SyncResult
from .predictors import KronosPredictor
from .providers import ProviderError, build_default_providers
from .storage import CandleStore
from .sync import DataSyncService


load_dotenv()


def create_app() -> FastAPI:
    app = FastAPI(title=os.getenv("APP_SITE_TITLE", "土豆A股预测研究"), version="0.1.0")
    _configure_cors(app)
    store = CandleStore(os.getenv("KLINE_DB_PATH", "data/candles.db"))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _index_html()

    @app.get("/config.js", response_class=PlainTextResponse)
    def config_js() -> PlainTextResponse:
        payload = json.dumps(_client_config(), ensure_ascii=False)
        return PlainTextResponse(f"window.KRONOS_CONFIG = {payload};\n", media_type="application/javascript")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "backend": "kronos", "db": str(store.db_path)}

    @app.post("/api/sync/{symbol}")
    def sync_symbol(symbol: str) -> dict[str, object]:
        service = DataSyncService(store=store, providers=build_default_providers())
        try:
            return service.sync_symbol(symbol).__dict__
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/predict/{symbol}")
    def predict_symbol(
        symbol: str,
        horizon: int = 5,
        paths: int = 3,
        lookback: int = 512,
        auto_sync: bool = True,
    ) -> dict[str, object]:
        sync_info = {"attempted": False, "updated": False}
        if auto_sync:
            sync_info = _auto_sync_symbol(store, symbol)

        candles = store.get_latest(symbol, limit=lookback)
        if len(candles) < 2:
            if sync_info.get("warning"):
                raise HTTPException(
                    status_code=502,
                    detail=f"failed to sync latest data and no usable local K-line data: {sync_info['warning']}",
                )
            raise HTTPException(status_code=404, detail="not enough local K-line data; no cached history was found")
        try:
            predictor = _build_predictor()
            result = predictor.predict(symbol, candles, horizon=horizon, paths=paths)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            **result.to_dict(),
            "history": [candle.to_dict() for candle in candles[-120:]],
            "analysis": _build_prediction_analysis(candles, result, horizon),
            "lookback": len(candles),
            "sync": sync_info,
        }

    return app

def _index_html() -> str:
    return Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")


def _client_config() -> dict[str, str]:
    return {
        "apiBaseUrl": os.getenv("APP_API_BASE_URL", "").rstrip("/"),
        "siteTitle": os.getenv("APP_SITE_TITLE", "土豆A股预测研究"),
    }


def _build_predictor() -> KronosPredictor:
    return KronosPredictor(
        model_name=os.getenv("KRONOS_MODEL", "NeoQuasar/Kronos-small"),
        tokenizer_name=os.getenv("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base"),
        device=os.getenv("KRONOS_DEVICE", "cpu"),
    )


def _build_prediction_analysis(candles: list, result, horizon: int) -> dict[str, object]:
    last_candle = candles[-1]
    last_close = float(last_candle.close)

    end_closes: list[float] = []
    projected_volatilities: list[float] = []
    for path in result.paths:
        closes = [float(point.close) for point in path.points]
        if not closes:
            continue
        end_closes.append(closes[-1])
        projected_volatilities.append(_mean_abs_return([last_close, *closes]))

    if not end_closes:
        return {
            "horizon": int(horizon),
            "lastDate": last_candle.date.isoformat(),
            "lastClose": last_close,
            "pathCount": 0,
            "signal": "neutral",
            "signalLabel": "震荡",
            "confidence": 0.0,
            "upsideProbability": 0.0,
            "downsideProbability": 0.0,
            "flatProbability": 1.0,
            "volatilityAmplificationProbability": 0.0,
            "meanProjectedClose": last_close,
            "meanProjectedReturn": 0.0,
            "projectedCloseLow": last_close,
            "projectedCloseHigh": last_close,
        }

    path_count = len(end_closes)
    upside_probability = sum(close > last_close for close in end_closes) / path_count
    downside_probability = sum(close < last_close for close in end_closes) / path_count
    flat_probability = max(0.0, 1.0 - upside_probability - downside_probability)
    mean_projected_close = sum(end_closes) / path_count
    mean_projected_return = ((mean_projected_close - last_close) / last_close) if last_close > 0 else 0.0

    recent_history = candles[-min(len(candles), 21) :]
    historical_volatility = _mean_abs_return([float(candle.close) for candle in recent_history])
    volatility_amplification_probability = (
        sum(volatility > historical_volatility for volatility in projected_volatilities) / len(projected_volatilities)
        if projected_volatilities
        else 0.0
    )

    signal, signal_label = _signal_from_probability(upside_probability)
    return {
        "horizon": int(horizon),
        "lastDate": last_candle.date.isoformat(),
        "lastClose": last_close,
        "pathCount": path_count,
        "signal": signal,
        "signalLabel": signal_label,
        "confidence": min(1.0, abs(upside_probability - 0.5) * 2.0),
        "upsideProbability": upside_probability,
        "downsideProbability": downside_probability,
        "flatProbability": flat_probability,
        "volatilityAmplificationProbability": volatility_amplification_probability,
        "meanProjectedClose": mean_projected_close,
        "meanProjectedReturn": mean_projected_return,
        "projectedCloseLow": min(end_closes),
        "projectedCloseHigh": max(end_closes),
    }


def _auto_sync_symbol(store: CandleStore, symbol: str) -> dict[str, object]:
    service = DataSyncService(store=store, providers=build_default_providers())
    try:
        result: SyncResult = service.sync_symbol(symbol)
        return {
            "attempted": True,
            "updated": True,
            "provider": result.provider,
            "rows": result.rows,
        }
    except ProviderError as exc:
        return {
            "attempted": True,
            "updated": False,
            "warning": str(exc),
        }


def _mean_abs_return(values: list[float]) -> float:
    returns: list[float] = []
    for prev, current in zip(values, values[1:]):
        if prev > 0:
            returns.append(abs((current - prev) / prev))
    return sum(returns) / len(returns) if returns else 0.0


def _signal_from_probability(upside_probability: float) -> tuple[str, str]:
    if upside_probability >= 0.62:
        return "bullish", "看涨"
    if upside_probability <= 0.38:
        return "bearish", "看跌"
    return "neutral", "震荡"


def _configure_cors(app: FastAPI) -> None:
    raw_origins = os.getenv("APP_ALLOW_ORIGINS", "")
    if not raw_origins:
        return

    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    if not origins:
        return

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )


app = create_app()

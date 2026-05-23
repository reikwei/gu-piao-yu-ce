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

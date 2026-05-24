import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from kronos_mvp.api import create_app
from kronos_mvp.models import Candle, ForecastPath, PredictionPoint, PredictionResult, SyncResult
from kronos_mvp.providers import ProviderError


def _sample_candles() -> list[Candle]:
    return [
        Candle(date=date(2026, 5, 21), open=10, high=11, low=9, close=10.5, volume=100, amount=1000),
        Candle(date=date(2026, 5, 22), open=10.5, high=11.5, low=10, close=11, volume=110, amount=1210),
    ]


def _sample_prediction_result() -> PredictionResult:
    return PredictionResult(
        symbol="600519",
        backend="kronos",
        paths=[
            ForecastPath(
                name="kronos_path_1",
                points=[PredictionPoint(date=date(2026, 5, 25), open=11.1, high=11.5, low=10.8, close=11.3)],
            )
        ],
    )


class FakeStore:
    def __init__(self, *args, candles: list[Candle] | None = None, empty_first: bool = False, **kwargs):
        self.db_path = Path("fake.db")
        self.candles = candles or _sample_candles()
        self.empty_first = empty_first
        self.calls = 0

    def get_latest(self, symbol: str, limit: int = 512) -> list[Candle]:
        self.calls += 1
        if self.empty_first and self.calls == 1:
            return []
        return list(self.candles)


class ApiTests(unittest.TestCase):
    def test_root_page_references_runtime_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"KLINE_DB_PATH": str(Path(tmp) / "candles.db")}, clear=False):
                client = TestClient(create_app())

                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("./config.js", response.text)
        self.assertIn("土豆A股预测研究", response.text)
        self.assertIn("请输入登录密码", response.text)
        self.assertIn("确认密码以后，才会进入正常首页。", response.text)
        self.assertIn("返回首页", response.text)
        self.assertIn("点击保存预测截图", response.text)
        self.assertIn("未来 7 个交易日", response.text)
        self.assertNotIn("Kronos Probability Desk", response.text)
        self.assertNotIn('id="paths"', response.text)

    def test_config_js_uses_environment_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "KLINE_DB_PATH": str(Path(tmp) / "candles.db"),
                "APP_API_BASE_URL": "https://api.example.com",
                "APP_SITE_TITLE": "土豆A股预测研究",
            }
            with patch.dict(os.environ, env, clear=False):
                client = TestClient(create_app())

                response = client.get("/config.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn("https://api.example.com", response.text)
        self.assertIn("土豆A股预测研究", response.text)

    def test_cors_allows_configured_pages_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "KLINE_DB_PATH": str(Path(tmp) / "candles.db"),
                "APP_ALLOW_ORIGINS": "https://stocks.example.com",
            }
            with patch.dict(os.environ, env, clear=False):
                client = TestClient(create_app())

                response = client.options(
                    "/api/predict/600519",
                    headers={
                        "Origin": "https://stocks.example.com",
                        "Access-Control-Request-Method": "GET",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "https://stocks.example.com")

    def test_predict_auto_syncs_before_prediction_when_cache_is_missing(self):
        store = FakeStore(empty_first=True)
        sync_service = Mock()
        def sync_then_fill_cache(symbol: str) -> SyncResult:
            store.empty_first = False
            return SyncResult(symbol=symbol, provider="baostock", rows=2)

        sync_service.sync_symbol.side_effect = sync_then_fill_cache
        predictor = Mock()
        predictor.predict.return_value = _sample_prediction_result()

        with patch("kronos_mvp.api.CandleStore", return_value=store), patch(
            "kronos_mvp.api.DataSyncService", return_value=sync_service
        ), patch("kronos_mvp.api.KronosPredictor", return_value=predictor):
            client = TestClient(create_app())
            response = client.get("/api/predict/600519?horizon=1&paths=3")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sync"]["updated"])
        self.assertEqual(response.json()["sync"]["provider"], "baostock")
        self.assertEqual(store.calls, 1)
        sync_service.sync_symbol.assert_called_once_with("600519")
        predictor.predict.assert_called_once()

    def test_predict_uses_cached_data_when_auto_sync_fails(self):
        store = FakeStore()
        sync_service = Mock()
        sync_service.sync_symbol.side_effect = ProviderError("offline")
        predictor = Mock()
        predictor.predict.return_value = _sample_prediction_result()

        with patch("kronos_mvp.api.CandleStore", return_value=store), patch(
            "kronos_mvp.api.DataSyncService", return_value=sync_service
        ), patch("kronos_mvp.api.KronosPredictor", return_value=predictor):
            client = TestClient(create_app())
            response = client.get("/api/predict/600519?horizon=1&paths=3")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sync"]["warning"], "offline")
        predictor.predict.assert_called_once()

    def test_predict_response_includes_probability_analysis(self):
        store = FakeStore()
        predictor = Mock()
        predictor.predict.return_value = _sample_prediction_result()

        with patch("kronos_mvp.api.CandleStore", return_value=store), patch(
            "kronos_mvp.api.KronosPredictor", return_value=predictor
        ):
            client = TestClient(create_app())
            response = client.get("/api/predict/600519?horizon=1&paths=3&auto_sync=false")

        self.assertEqual(response.status_code, 200)
        analysis = response.json()["analysis"]
        self.assertEqual(analysis["horizon"], 1)
        self.assertEqual(analysis["signal"], "bullish")
        self.assertEqual(analysis["signalLabel"], "看涨")
        self.assertEqual(analysis["pathCount"], 1)
        self.assertEqual(analysis["upsideProbability"], 1.0)
        self.assertGreater(analysis["meanProjectedClose"], analysis["lastClose"])

    def test_predict_requires_password_when_access_protected(self):
        store = FakeStore()
        predictor = Mock()
        predictor.predict.return_value = _sample_prediction_result()

        with patch.dict(os.environ, {"APP_ACCESS_PASSWORD": "secret-pass"}, clear=False), patch(
            "kronos_mvp.api.CandleStore", return_value=store
        ), patch("kronos_mvp.api.KronosPredictor", return_value=predictor):
            client = TestClient(create_app())

            status_response = client.get("/auth/status")
            unauthorized = client.get("/api/predict/600519?auto_sync=false")
            wrong = client.post("/auth/login", json={"password": "wrong"})
            correct = client.post("/auth/login", json={"password": "secret-pass"})
            authorized = client.get("/api/predict/600519?auto_sync=false")

        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.json()["protected"])
        self.assertFalse(status_response.json()["authorized"])
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(authorized.status_code, 200)


if __name__ == "__main__":
    unittest.main()
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from kronos_mvp.api import _build_prediction_analysis, _build_predictor, _cached_predictor, create_app
from kronos_mvp.funds import FundFactor
from kronos_mvp.models import Candle, ForecastPath, PredictionPoint, PredictionResult, SyncResult
from kronos_mvp.providers import ProviderError
from kronos_mvp.relative_strength import RelativeStrengthStore, SymbolIndustry, industry_key_from_name


def _sample_candles() -> list[Candle]:
    return [
        Candle(date=date(2026, 5, 21), open=10, high=11, low=9, close=10.5, volume=100, amount=1000, turnover=1.1),
        Candle(date=date(2026, 5, 22), open=10.5, high=11.5, low=10, close=11, volume=110, amount=1210, turnover=1.2),
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


def _sample_price_volume_confirmed_candles() -> list[Candle]:
    return [
        Candle(date=date(2026, 5, 12), open=10.00, high=10.18, low=9.96, close=10.12, volume=100, amount=1012, turnover=0.82),
        Candle(date=date(2026, 5, 13), open=10.10, high=10.26, low=10.02, close=10.21, volume=104, amount=1062, turnover=0.84),
        Candle(date=date(2026, 5, 14), open=10.20, high=10.34, low=10.08, close=10.30, volume=108, amount=1112, turnover=0.86),
        Candle(date=date(2026, 5, 15), open=10.28, high=10.42, low=10.16, close=10.38, volume=112, amount=1163, turnover=0.89),
        Candle(date=date(2026, 5, 16), open=10.36, high=10.50, low=10.24, close=10.47, volume=116, amount=1215, turnover=0.93),
        Candle(date=date(2026, 5, 19), open=10.46, high=10.63, low=10.34, close=10.58, volume=120, amount=1269, turnover=0.97),
        Candle(date=date(2026, 5, 20), open=10.56, high=10.76, low=10.48, close=10.70, volume=126, amount=1348, turnover=1.01),
        Candle(date=date(2026, 5, 21), open=10.68, high=10.88, low=10.60, close=10.82, volume=132, amount=1428, turnover=1.05),
        Candle(date=date(2026, 5, 22), open=10.80, high=11.02, low=10.72, close=10.95, volume=138, amount=1511, turnover=1.10),
        Candle(date=date(2026, 5, 23), open=10.98, high=11.46, low=10.94, close=11.38, volume=230, amount=2617, turnover=1.92),
    ]


def _sample_price_volume_weak_candles() -> list[Candle]:
    return [
        Candle(date=date(2026, 5, 12), open=11.80, high=11.88, low=11.62, close=11.70, volume=118, amount=1381, turnover=1.46),
        Candle(date=date(2026, 5, 13), open=11.72, high=11.78, low=11.48, close=11.56, volume=120, amount=1387, turnover=1.42),
        Candle(date=date(2026, 5, 14), open=11.58, high=11.64, low=11.32, close=11.40, volume=122, amount=1391, turnover=1.38),
        Candle(date=date(2026, 5, 15), open=11.42, high=11.48, low=11.18, close=11.28, volume=124, amount=1399, turnover=1.33),
        Candle(date=date(2026, 5, 16), open=11.30, high=11.36, low=11.02, close=11.10, volume=126, amount=1399, turnover=1.28),
        Candle(date=date(2026, 5, 19), open=11.08, high=11.14, low=10.84, close=10.96, volume=128, amount=1402, turnover=1.24),
        Candle(date=date(2026, 5, 20), open=10.98, high=11.02, low=10.72, close=10.82, volume=130, amount=1407, turnover=1.20),
        Candle(date=date(2026, 5, 21), open=10.84, high=10.90, low=10.58, close=10.70, volume=132, amount=1412, turnover=1.16),
        Candle(date=date(2026, 5, 22), open=10.72, high=10.78, low=10.46, close=10.56, volume=134, amount=1415, turnover=1.12),
        Candle(date=date(2026, 5, 23), open=10.54, high=10.58, low=10.02, close=10.08, volume=228, amount=1296, turnover=0.78),
    ]


def _sample_fund_factors() -> list[FundFactor]:
    return [
        FundFactor(symbol="600519", trade_date=date(2026, 5, 12), fund_net_inflow=5.0e7, fund_net_inflow_ratio=1.2, margin_balance=8.10e9, margin_buy_amount=2.4e8),
        FundFactor(symbol="600519", trade_date=date(2026, 5, 13), fund_net_inflow=4.5e7, fund_net_inflow_ratio=1.1, margin_balance=8.18e9, margin_buy_amount=2.5e8),
        FundFactor(symbol="600519", trade_date=date(2026, 5, 14), fund_net_inflow=6.2e7, fund_net_inflow_ratio=1.8, margin_balance=8.25e9, margin_buy_amount=2.7e8),
        FundFactor(symbol="600519", trade_date=date(2026, 5, 15), fund_net_inflow=7.0e7, fund_net_inflow_ratio=2.0, margin_balance=8.36e9, margin_buy_amount=2.9e8),
        FundFactor(symbol="600519", trade_date=date(2026, 5, 16), fund_net_inflow=7.5e7, fund_net_inflow_ratio=2.1, margin_balance=8.48e9, margin_buy_amount=3.0e8),
        FundFactor(symbol="600519", trade_date=date(2026, 5, 19), fund_net_inflow=8.0e7, fund_net_inflow_ratio=2.4, margin_balance=8.60e9, margin_buy_amount=3.1e8),
        FundFactor(symbol="600519", trade_date=date(2026, 5, 20), fund_net_inflow=9.5e7, fund_net_inflow_ratio=2.7, margin_balance=8.76e9, margin_buy_amount=3.3e8),
        FundFactor(symbol="600519", trade_date=date(2026, 5, 21), fund_net_inflow=1.0e8, fund_net_inflow_ratio=2.9, margin_balance=8.84e9, margin_buy_amount=3.5e8),
        FundFactor(symbol="600519", trade_date=date(2026, 5, 22), fund_net_inflow=1.2e8, fund_net_inflow_ratio=3.1, margin_balance=8.92e9, margin_buy_amount=3.8e8),
        FundFactor(symbol="600519", trade_date=date(2026, 5, 23), fund_net_inflow=1.5e8, fund_net_inflow_ratio=3.6, margin_balance=9.0e9, margin_buy_amount=4.2e8),
    ]


class FakeStore:
    def __init__(self, *args, candles: list[Candle] | None = None, empty_first: bool = False, last_updated_at: str | None = None, **kwargs):
        self.db_path = Path("fake.db")
        self.candles = candles or _sample_candles()
        self.empty_first = empty_first
        self.calls = 0
        self.last_updated_at = last_updated_at

    def get_latest(self, symbol: str, limit: int = 512) -> list[Candle]:
        self.calls += 1
        if self.empty_first and self.calls == 1:
            return []
        return list(self.candles)

    def get_last_updated_at(self):
        return self.last_updated_at


class FakeFundStore:
    def __init__(self, *args, factors: list[FundFactor] | None = None, last_updated_at: str | None = None, **kwargs):
        self.db_path = Path("fake_fund.db")
        self.factors = list(factors or [])
        self.last_updated_at = last_updated_at

    def get_latest(self, symbol: str, limit: int = 15) -> list[FundFactor]:
        return [factor for factor in self.factors if factor.symbol == symbol][-limit:]

    def get_latest_trade_date(self):
        if not self.factors:
            return None
        return max(factor.trade_date for factor in self.factors)

    def get_last_updated_at(self):
        return self.last_updated_at


class FakeRelativeStore:
    def __init__(self, *args, last_updated_at: str | None = None, **kwargs):
        self.db_path = Path("fake_relative.db")
        self.last_updated_at = last_updated_at

    def get_last_updated_at(self):
        return self.last_updated_at

    def get_symbol_industry(self, symbol: str):
        return None

    def get_latest(self, symbol: str, limit: int = 512):
        return []


class FakeNewsStore:
    def __init__(self, *args, last_updated_at: str | None = None, **kwargs):
        self.db_path = Path("fake_news.db")
        self.last_updated_at = last_updated_at

    def get_last_updated_at(self):
        return self.last_updated_at

    def get_latest(self, symbol: str, limit: int = 8, max_age_days: int = 3):
        return []

    def get_symbol_last_updated_at(self, symbol: str):
        return self.last_updated_at


def _test_env(tmp: str | Path, **overrides: str) -> dict[str, str]:
    path = Path(tmp)
    env = {
        "KLINE_DB_PATH": str(path / "candles.db"),
        "RELATIVE_DB_PATH": str(path / "relative_strength.db"),
        "APP_DB_PATH": str(path / "app.db"),
        "APP_ACCESS_PASSWORD": "",
        "ADMIN_PASSWORD": "",
        "SAILA_ID": "",
        "SAILA_KEY": "",
    }
    env.update(overrides)
    return env


def _register(client: TestClient, username: str = "alice", password: str = "secret123", contact: str | None = None) -> dict:
    payload = {"username": username, "password": password}
    if contact is not None:
        payload["contact"] = contact
    response = client.post("/api/auth/register", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["user"]


class ApiTests(unittest.TestCase):
    def tearDown(self):
        _cached_predictor.cache_clear()

    def test_root_page_references_runtime_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _test_env(tmp), clear=False):
                client = TestClient(create_app())

                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("./config.js", response.text)
        self.assertIn("土豆A股预测研究", response.text)
        self.assertIn("登录账户", response.text)
        self.assertIn("注册账号", response.text)
        self.assertIn("确认密码", response.text)
        self.assertIn("联系方式（可选）", response.text)
        self.assertIn("QQ / 微信 / 电话都可以，不填也行", response.text)
        self.assertIn("确认注册", response.text)
        self.assertIn("取消", response.text)
        self.assertIn("id='register-status'", response.text)
        self.assertIn("填写信息后点击确认注册。", response.text)
        self.assertIn("进入账户中心", response.text)
        self.assertIn("home-predict-overlay", response.text)
        self.assertIn("正在生成预测分析", response.text)
        self.assertIn("预测标的", response.text)
        self.assertIn("value='sh000001'", response.text)
        self.assertIn("A股上证指数", response.text)
        self.assertIn("大盘结构分析", response.text)
        self.assertIn("指数预测页", response.text)
        self.assertIn("id='home-data-freshness'", response.text)
        self.assertIn("最新数据更新时间：读取中...", response.text)
        self.assertIn("/api/data-freshness", response.text)
        self.assertIn("预测超时……", response.text)
        self.assertIn("系统错误，请联系管理WX 7354280", response.text)
        self.assertIn("你还剩余", response.text)
        self.assertIn("包年不限制查询。详情查阅 账户中心。", response.text)
        self.assertIn("你的免费次数已用完，请到账户中心充值。", response.text)
        self.assertIn("进入账户中心后，可选择小额充值或充值包年。", response.text)
        self.assertIn("home-predict-overlay-link", response.text)
        self.assertIn("open-account-center", response.text)
        self.assertNotIn("每次普通查询扣", response.text)
        self.assertIn("土豆A股预测研究院用户中心", response.text)
        self.assertIn("欢迎进入用户后台", response.text)
        self.assertIn("小额充值", response.text)
        self.assertIn("充值余额", response.text)
        self.assertIn("value='alipay'", response.text)
        self.assertIn("支付宝", response.text)
        self.assertNotIn("微信支付", response.text)
        self.assertNotIn("USDT", response.text)
        self.assertNotIn("wxpay", response.text)
        self.assertNotIn("usdt", response.text)
        self.assertIn("充值包年", response.text)
        self.assertIn("余额转包年", response.text)
        self.assertIn("floating-message", response.text)
        self.assertIn("关闭提示", response.text)
        self.assertIn("修改密码", response.text)
        self.assertIn("输入原始密码", response.text)
        self.assertIn("输入新密码", response.text)
        self.assertIn("确认新密码", response.text)
        self.assertIn("handleAccountTabClick", response.text)
        self.assertIn("name === 'logout'", response.text)
        self.assertIn("资金面分析", response.text)
        self.assertIn("综合结论", response.text)
        self.assertIn("消息面层", response.text)
        self.assertIn("相对强弱层", response.text)
        self.assertIn("重新加载资金面数据", response.text)
        self.assertIn("数据更新时间", response.text)
        self.assertIn("样本交易日数", response.text)
        self.assertIn("土豆A股预测研究院", response.text)
        self.assertIn("https://jdn.cc.cd", response.text)
        self.assertIn("3日累计主力净流入", response.text)
        self.assertIn("5日累计主力净流入", response.text)
        self.assertIn("10日累计主力净流入", response.text)
        self.assertIn("连续净流入天数", response.text)
        self.assertIn("融资余额3日斜率", response.text)
        self.assertIn("融资余额3日加速度", response.text)
        self.assertIn("结论类型", response.text)
        self.assertIn("getLatestAvailableMarginRecord", response.text)
        self.assertIn("最新交易日暂未拿到融资明细，当前展示最近一次可用融资数据", response.text)
        self.assertNotIn("id='fund-view'", response.text)
        self.assertNotIn("id='fund-analysis-button'", response.text)
        self.assertIn("后台用户管理", response.text)
        self.assertIn("联系方式", response.text)
        self.assertIn("重置密码", response.text)
        self.assertIn("返回首页", response.text)
        self.assertIn("点击保存预测截图", response.text)
        self.assertIn("未来 7 个交易日", response.text)
        self.assertNotIn("Kronos Probability Desk", response.text)
        self.assertNotIn('id="paths"', response.text)

    def test_config_js_uses_environment_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **_test_env(tmp),
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
                **_test_env(tmp),
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
        self.assertEqual(response.headers.get("access-control-allow-credentials"), "true")

    def test_data_freshness_endpoint_prefers_latest_source_update_time(self):
        store = FakeStore(last_updated_at="2026-05-25T16:30:00+08:00")
        fund_store = FakeFundStore(
            factors=_sample_fund_factors(),
            last_updated_at="2026-05-25T18:09:00+08:00",
        )
        relative_store = FakeRelativeStore(last_updated_at="2026-05-25T17:00:00+08:00")
        news_store = FakeNewsStore(last_updated_at="2026-05-25T15:00:00+08:00")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.CandleStore", return_value=store
        ), patch("kronos_mvp.api.FundFactorStore", return_value=fund_store), patch(
            "kronos_mvp.api.RelativeStrengthStore", return_value=relative_store
        ), patch(
            "kronos_mvp.api.StockNewsStore", return_value=news_store
        ):
            client = TestClient(create_app())

            response = client.get("/api/data-freshness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["updatedAt"], "2026-05-25T18:09:00+08:00")
        self.assertEqual(payload["klineUpdatedAt"], "2026-05-25T16:30:00+08:00")
        self.assertEqual(payload["fundUpdatedAt"], "2026-05-25T18:09:00+08:00")
        self.assertEqual(payload["relativeUpdatedAt"], "2026-05-25T17:00:00+08:00")
        self.assertEqual(payload["newsUpdatedAt"], "2026-05-25T15:00:00+08:00")
        self.assertEqual(payload["fundLatestTradeDate"], "2026-05-23")

    def test_register_grants_ten_free_credits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _test_env(tmp), clear=False):
                client = TestClient(create_app())

                user = _register(client)
                me = client.get("/api/me")

        self.assertEqual(user["freeCreditsRemaining"], 10)
        self.assertEqual(user["balanceCents"], 0)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["username"], "alice")

    def test_register_accepts_optional_contact(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _test_env(tmp, APP_ACCESS_PASSWORD="secret-pass"), clear=False):
                app = create_app()
                user_client = TestClient(app)
                admin_client = TestClient(app)

                user = _register(user_client, username="alice", password="secret123", contact="微信 abc123")
                me = user_client.get("/api/me")
                admin_client.post("/api/auth/login", json={"username": "admin", "password": "secret-pass"})
                admin_users = admin_client.get("/api/admin/users")

        self.assertEqual(user["contact"], "微信 abc123")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["contact"], "微信 abc123")
        self.assertEqual(admin_users.status_code, 200)
        self.assertEqual(
            next(item for item in admin_users.json()["users"] if item["username"] == "alice")["contact"],
            "微信 abc123",
        )

    def test_payment_return_auto_redirects_to_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _test_env(tmp, APP_PUBLIC_BASE_URL="https://stocks.example.com"), clear=False):
                client = TestClient(create_app())

                response = client.get("/api/payments/return")

        self.assertEqual(response.status_code, 200)
        self.assertIn("支付结果正在确认中", response.text)
        self.assertIn("正在自动返回登录后的首页", response.text)
        self.assertIn("window.location.replace", response.text)
        self.assertIn("https://stocks.example.com/?payment=return", response.text)

    def test_payment_order_rejects_unsupported_pay_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _test_env(tmp), clear=False):
                client = TestClient(create_app())
                _register(client)

                response = client.post(
                    "/api/payments/orders",
                    json={"amountYuan": "1", "orderType": "balance", "payType": "wxpay"},
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "当前仅支持支付宝充值。")

    def test_user_can_change_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _test_env(tmp), clear=False):
                client = TestClient(create_app())
                _register(client, username="alice", password="oldpass123")

                wrong_current = client.post(
                    "/api/me/change-password",
                    json={"currentPassword": "wrongpass", "newPassword": "newpass123"},
                )
                changed = client.post(
                    "/api/me/change-password",
                    json={"currentPassword": "oldpass123", "newPassword": "newpass123"},
                )
                client.post("/api/auth/logout")
                old_login = client.post("/api/auth/login", json={"username": "alice", "password": "oldpass123"})
                new_login = client.post("/api/auth/login", json={"username": "alice", "password": "newpass123"})

        self.assertEqual(wrong_current.status_code, 401)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200)

    def test_admin_can_reset_user_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _test_env(tmp, APP_ACCESS_PASSWORD="secret-pass"), clear=False):
                app = create_app()
                user_client = TestClient(app)
                admin_client = TestClient(app)
                user = _register(user_client, username="bob", password="oldpass123")
                me_before_reset = user_client.get("/api/me")

                admin_login = admin_client.post("/api/auth/login", json={"username": "admin", "password": "secret-pass"})
                reset = admin_client.post(
                    f"/api/admin/users/{user['id']}/reset-password",
                    json={"newPassword": "newpass123"},
                )
                me_after_reset = user_client.get("/api/me")
                old_login = user_client.post("/api/auth/login", json={"username": "bob", "password": "oldpass123"})
                new_login = user_client.post("/api/auth/login", json={"username": "bob", "password": "newpass123"})

        self.assertEqual(me_before_reset.status_code, 200)
        self.assertEqual(admin_login.status_code, 200)
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(me_after_reset.status_code, 401)
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200)

    def test_predict_auto_syncs_before_prediction_when_cache_is_missing(self):
        store = FakeStore(empty_first=True)
        sync_service = Mock()
        def sync_then_fill_cache(symbol: str) -> SyncResult:
            store.empty_first = False
            return SyncResult(symbol=symbol, provider="baostock", rows=2)

        sync_service.sync_symbol.side_effect = sync_then_fill_cache
        predictor = Mock()
        predictor.predict.return_value = _sample_prediction_result()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.CandleStore", return_value=store
        ), patch("kronos_mvp.api.DataSyncService", return_value=sync_service), patch(
            "kronos_mvp.api.KronosPredictor", return_value=predictor
        ), patch("kronos_mvp.api._auto_sync_relative_strength", return_value={"attempted": False, "updated": False}):
            client = TestClient(create_app())
            _register(client)
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

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.CandleStore", return_value=store
        ), patch("kronos_mvp.api.DataSyncService", return_value=sync_service), patch(
            "kronos_mvp.api.KronosPredictor", return_value=predictor
        ), patch("kronos_mvp.api._auto_sync_relative_strength", return_value={"attempted": False, "updated": False}):
            client = TestClient(create_app())
            _register(client)
            response = client.get("/api/predict/600519?horizon=1&paths=3")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sync"]["warning"], "offline")
        predictor.predict.assert_called_once()

    def test_predict_response_includes_probability_analysis(self):
        store = FakeStore()
        predictor = Mock()
        predictor.predict.return_value = _sample_prediction_result()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.CandleStore", return_value=store
        ), patch("kronos_mvp.api.KronosPredictor", return_value=predictor), patch(
            "kronos_mvp.api.lookup_a_share_name", return_value="贵州茅台"
        ):
            client = TestClient(create_app())
            _register(client)
            response = client.get("/api/predict/600519?horizon=1&paths=3&auto_sync=false")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["symbolName"], "贵州茅台")
        analysis = response.json()["analysis"]
        self.assertEqual(analysis["horizon"], 1)
        self.assertEqual(analysis["signal"], "bullish")
        self.assertEqual(analysis["signalLabel"], "看涨")
        self.assertEqual(analysis["pathCount"], 1)
        self.assertEqual(analysis["upsideProbability"], 1.0)
        self.assertGreater(analysis["meanProjectedClose"], analysis["lastClose"])
        self.assertIn("priceVolumeConfirmation", analysis)
        self.assertIn("relativeStrength", analysis)
        self.assertIn("newsSentiment", analysis)
        self.assertIn("upsideProbabilityBlended", analysis)
        self.assertIn("score", analysis["priceVolumeConfirmation"])
        self.assertEqual(response.json()["billing"]["chargeType"], "free_credit")
        self.assertEqual(response.json()["billing"]["me"]["freeCreditsRemaining"], 9)

    def test_predict_market_index_returns_index_profile(self):
        store = FakeStore(candles=_sample_price_volume_confirmed_candles())
        predictor = Mock()
        predictor.predict.return_value = PredictionResult(
            symbol="sh000001",
            backend="kronos",
            paths=[
                ForecastPath(
                    name="kronos_path_1",
                    points=[PredictionPoint(date=date(2026, 5, 26), open=11.4, high=11.8, low=11.2, close=11.7)],
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.CandleStore", return_value=store
        ), patch("kronos_mvp.api.KronosPredictor", return_value=predictor):
            client = TestClient(create_app())
            _register(client)
            response = client.get("/api/predict/sh000001?horizon=1&paths=3&auto_sync=false")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["symbol"], "sh000001")
        self.assertEqual(payload["symbolName"], "上证指数")
        self.assertEqual(payload["instrumentType"], "market_index")
        self.assertEqual(payload["instrument"]["label"], "A股上证指数")
        self.assertTrue(payload["analysis"]["marketIndex"]["available"])
        self.assertIn("大盘", payload["analysis"]["marketIndex"]["signalLabel"])
        self.assertFalse(payload["analysis"]["relativeStrength"]["available"])
        self.assertEqual(payload["sync"]["news"], {"attempted": False, "updated": False})
        predictor.predict.assert_called_once()
        self.assertEqual(predictor.predict.call_args.args[0], "sh000001")

    def test_predict_surfaces_news_warning_when_provider_returns_empty(self):
        store = FakeStore()
        predictor = Mock()
        predictor.predict.return_value = _sample_prediction_result()
        news_store = FakeNewsStore(last_updated_at=None)
        empty_news_result = Mock()
        empty_news_result.rows = 0
        empty_news_result.to_dict.return_value = {"symbol": "600835", "provider": "none", "rows": 0}

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.CandleStore", return_value=store
        ), patch("kronos_mvp.api.KronosPredictor", return_value=predictor), patch(
            "kronos_mvp.api.StockNewsStore", return_value=news_store
        ), patch("kronos_mvp.api.StockNewsSyncService") as news_service_cls, patch(
            "kronos_mvp.api.build_default_news_providers", return_value=[]
        ):
            news_service = Mock()
            news_service.sync_symbol.return_value = empty_news_result
            news_service_cls.return_value = news_service

            client = TestClient(create_app())
            _register(client)
            response = client.get("/api/predict/600835?horizon=1&paths=3&auto_sync=false")

        self.assertEqual(response.status_code, 200)
        news_sync = response.json()["sync"]["news"]
        self.assertTrue(news_sync["attempted"])
        self.assertFalse(news_sync["updated"])
        self.assertIn("warning", news_sync)

    def test_build_predictor_reuses_cached_instance(self):
        predictor = Mock()

        with patch.dict(
            os.environ,
            {
                "KRONOS_MODEL": "model-a",
                "KRONOS_TOKENIZER": "tokenizer-a",
                "KRONOS_DEVICE": "cpu",
            },
            clear=False,
        ), patch("kronos_mvp.api.KronosPredictor", return_value=predictor) as predictor_cls:
            first = _build_predictor()
            second = _build_predictor()

        self.assertIs(first, second)
        predictor_cls.assert_called_once_with(
            model_name="model-a",
            tokenizer_name="tokenizer-a",
            device="cpu",
        )

    def test_startup_prewarms_predictor_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            _test_env(tmp, KRONOS_PREWARM_ON_STARTUP="1"),
            clear=False,
        ), patch("kronos_mvp.api._prewarm_predictor") as prewarm:
            with TestClient(create_app()) as client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        prewarm.assert_called_once_with()

    def test_predict_rate_limit_blocks_second_request_without_consuming_extra_credit(self):
        store = FakeStore()
        predictor = Mock()
        predictor.predict.return_value = _sample_prediction_result()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            _test_env(tmp, PREDICT_RATE_LIMIT_REQUESTS="1", PREDICT_RATE_LIMIT_WINDOW_SECONDS="60"),
            clear=False,
        ), patch("kronos_mvp.api.CandleStore", return_value=store), patch(
            "kronos_mvp.api.KronosPredictor", return_value=predictor
        ):
            client = TestClient(create_app())
            _register(client)
            first = client.get("/api/predict/600519?horizon=1&paths=3&auto_sync=false")
            second = client.get("/api/predict/600519?horizon=1&paths=3&auto_sync=false")
            me = client.get("/api/me")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.headers.get("retry-after"), "60")
        self.assertIn("预测请求过于频繁", second.json()["detail"])
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["freeCreditsRemaining"], 9)
        predictor.predict.assert_called_once()

    def test_build_prediction_analysis_handles_empty_candles(self):
        analysis = _build_prediction_analysis([], _sample_prediction_result(), horizon=1)

        self.assertEqual(analysis["signal"], "neutral")
        self.assertEqual(analysis["pathCount"], 0)
        self.assertIsNone(analysis["lastDate"])
        self.assertEqual(analysis["lastClose"], 0.0)
        self.assertEqual(analysis["priceVolumeConfirmation"]["signal"], "neutral")
        self.assertIn("newsSentiment", analysis)
        self.assertFalse(analysis["newsSentiment"]["available"])

    def test_build_prediction_analysis_includes_bullish_relative_strength(self):
        with tempfile.TemporaryDirectory() as tmp:
            relative_store = RelativeStrengthStore(Path(tmp) / "relative_strength.db")
            relative_store.upsert_symbol_industries(
                [
                    SymbolIndustry(
                        symbol="600519",
                        industry_key=industry_key_from_name("白酒"),
                        industry_name="白酒",
                        source="akshare",
                        updated_at="2026-05-25T16:30:00+08:00",
                    )
                ]
            )
            relative_store.upsert_candles(
                "benchmark:sh",
                [
                    Candle(date=date(2026, 5, 12), open=10.0, high=10.1, low=9.9, close=10.0, volume=800, amount=8000),
                    Candle(date=date(2026, 5, 13), open=10.02, high=10.12, low=9.98, close=10.05, volume=810, amount=8140),
                    Candle(date=date(2026, 5, 14), open=10.06, high=10.16, low=10.0, close=10.10, volume=820, amount=8280),
                    Candle(date=date(2026, 5, 15), open=10.10, high=10.20, low=10.04, close=10.16, volume=830, amount=8420),
                    Candle(date=date(2026, 5, 16), open=10.14, high=10.24, low=10.08, close=10.22, volume=840, amount=8570),
                    Candle(date=date(2026, 5, 19), open=10.20, high=10.30, low=10.12, close=10.28, volume=850, amount=8730),
                    Candle(date=date(2026, 5, 20), open=10.26, high=10.36, low=10.18, close=10.34, volume=860, amount=8890),
                    Candle(date=date(2026, 5, 21), open=10.30, high=10.40, low=10.24, close=10.40, volume=870, amount=9050),
                    Candle(date=date(2026, 5, 22), open=10.36, high=10.46, low=10.30, close=10.46, volume=880, amount=9200),
                    Candle(date=date(2026, 5, 23), open=10.42, high=10.52, low=10.36, close=10.52, volume=890, amount=9360),
                ],
            )
            relative_store.upsert_candles(
                "industry:白酒",
                [
                    Candle(date=date(2026, 5, 12), open=10.0, high=10.1, low=9.9, close=10.0, volume=900, amount=9000),
                    Candle(date=date(2026, 5, 13), open=10.03, high=10.13, low=9.99, close=10.04, volume=910, amount=9140),
                    Candle(date=date(2026, 5, 14), open=10.07, high=10.17, low=10.01, close=10.09, volume=920, amount=9280),
                    Candle(date=date(2026, 5, 15), open=10.10, high=10.20, low=10.04, close=10.13, volume=930, amount=9420),
                    Candle(date=date(2026, 5, 16), open=10.13, high=10.23, low=10.07, close=10.18, volume=940, amount=9570),
                    Candle(date=date(2026, 5, 19), open=10.18, high=10.28, low=10.12, close=10.23, volume=950, amount=9730),
                    Candle(date=date(2026, 5, 20), open=10.21, high=10.31, low=10.15, close=10.28, volume=960, amount=9890),
                    Candle(date=date(2026, 5, 21), open=10.26, high=10.36, low=10.20, close=10.33, volume=970, amount=10050),
                    Candle(date=date(2026, 5, 22), open=10.30, high=10.40, low=10.24, close=10.38, volume=980, amount=10200),
                    Candle(date=date(2026, 5, 23), open=10.34, high=10.44, low=10.28, close=10.43, volume=990, amount=10360),
                ],
            )

            analysis = _build_prediction_analysis(
                _sample_price_volume_confirmed_candles(),
                _sample_prediction_result(),
                horizon=1,
                symbol="600519",
                relative_strength_store=relative_store,
            )

        self.assertTrue(analysis["relativeStrength"]["available"])
        self.assertEqual(analysis["relativeStrength"]["signal"], "bullish")
        self.assertGreaterEqual(analysis["relativeStrength"]["score"], 65)

    def test_build_prediction_analysis_flags_bullish_price_volume_confirmation(self):
        analysis = _build_prediction_analysis(_sample_price_volume_confirmed_candles(), _sample_prediction_result(), horizon=1)

        confirmation = analysis["priceVolumeConfirmation"]
        self.assertEqual(confirmation["signal"], "bullish")
        self.assertGreaterEqual(confirmation["score"], 65)
        self.assertTrue(confirmation["metrics"]["isBreakout"])
        self.assertGreater(confirmation["metrics"]["volumeRatio5"], 1.2)
        self.assertGreater(confirmation["metrics"]["amountRatio5"], 1.4)
        self.assertGreater(confirmation["metrics"]["turnoverRatio5"], 1.4)

    def test_build_prediction_analysis_flags_bearish_price_volume_confirmation(self):
        bearish_result = PredictionResult(
            symbol="600519",
            backend="kronos",
            paths=[
                ForecastPath(
                    name="kronos_path_1",
                    points=[PredictionPoint(date=date(2026, 5, 26), open=10.02, high=10.08, low=9.84, close=9.92)],
                )
            ],
        )

        analysis = _build_prediction_analysis(_sample_price_volume_weak_candles(), bearish_result, horizon=1)

        confirmation = analysis["priceVolumeConfirmation"]
        self.assertEqual(confirmation["signal"], "bearish")
        self.assertLessEqual(confirmation["score"], 35)
        self.assertTrue(confirmation["metrics"]["isBreakdown"])
        self.assertLess(confirmation["metrics"]["turnoverRatio5"], 0.8)

    def test_fund_analysis_endpoint_returns_scored_summary(self):
        fund_store = FakeFundStore(factors=_sample_fund_factors())

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.FundFactorStore", return_value=fund_store
        ):
            client = TestClient(create_app())
            _register(client)
            response = client.get("/api/funds/600519")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["symbol"], "600519")
        self.assertEqual(payload["analysis"]["signalLabel"], "偏多")
        self.assertGreaterEqual(payload["analysis"]["score"], 70)
        self.assertEqual(payload["analysis"]["trendProfile"]["label"], "持续趋势")
        self.assertEqual(payload["analysis"]["flowMetrics"]["consecutiveInflowDays"], 10)
        self.assertAlmostEqual(payload["analysis"]["flowMetrics"]["netInflow10d"], 847000000.0)
        self.assertEqual(len(payload["history"]), 10)

    def test_fund_analysis_endpoint_rejects_market_index(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False):
            client = TestClient(create_app())
            _register(client)
            response = client.get("/api/funds/sh000001")

        self.assertEqual(response.status_code, 400)
        self.assertIn("指数预测页不使用个股资金面", response.json()["detail"])

    def test_fund_analysis_endpoint_auto_syncs_when_requested(self):
        fund_store = FakeFundStore(factors=[])
        sync_service = Mock()

        def sync_recent(history_days: int, trade_date: date | None = None):
            fund_store.factors = _sample_fund_factors()
            result = Mock()
            result.synced_trade_dates = (date(2026, 5, 23),)
            result.to_dict.return_value = {
                "targetDate": "2026-05-23",
                "requestedDays": history_days,
                "syncedDays": 1,
                "skippedDays": 14,
                "syncedTradeDates": ["2026-05-23"],
                "skippedTradeDates": [],
                "rows": 5191,
                "providers": ["akshare"],
            }
            return result

        sync_service.sync_recent.side_effect = sync_recent

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.FundFactorStore", return_value=fund_store
        ), patch("kronos_mvp.api.FundFactorSyncService", return_value=sync_service), patch(
            "kronos_mvp.api.build_default_fund_providers", return_value=[]
        ), patch("kronos_mvp.api.latest_a_share_trade_date", return_value=date(2026, 5, 23)):
            client = TestClient(create_app())
            _register(client)
            response = client.get("/api/funds/600519?auto_sync=true")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sync"]["attempted"])
        self.assertTrue(response.json()["sync"]["updated"])
        sync_service.sync_recent.assert_called_once_with(history_days=15, trade_date=date(2026, 5, 23))

    def test_predict_requires_login(self):
        store = FakeStore()
        predictor = Mock()
        predictor.predict.return_value = _sample_prediction_result()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.CandleStore", return_value=store
        ), patch("kronos_mvp.api.KronosPredictor", return_value=predictor):
            client = TestClient(create_app())

            status_response = client.get("/auth/status")
            unauthorized = client.get("/api/predict/600519?auto_sync=false")
            _register(client)
            authorized = client.get("/api/predict/600519?auto_sync=false")

        self.assertEqual(status_response.status_code, 200)
        self.assertTrue(status_response.json()["protected"])
        self.assertFalse(status_response.json()["authorized"])
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)

    def test_old_login_route_is_disabled_and_admin_uses_unified_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _test_env(tmp, APP_ACCESS_PASSWORD="secret-pass"), clear=False):
                client = TestClient(create_app())

                legacy = client.post("/auth/login", json={"password": "secret-pass"})
                correct = client.post("/api/auth/login", json={"username": "admin", "password": "secret-pass"})
                users = client.get("/api/admin/users")

        self.assertEqual(legacy.status_code, 410)
        self.assertIn("/api/auth/login", legacy.json()["detail"])
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(correct.json()["user"]["username"], "admin")
        self.assertTrue(correct.json()["user"]["isAdmin"])
        self.assertEqual(users.status_code, 200)


if __name__ == "__main__":
    unittest.main()
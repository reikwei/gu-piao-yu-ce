import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from kronos_mvp.api import create_app
from kronos_mvp.funds import FundFactor
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


def _test_env(tmp: str | Path, **overrides: str) -> dict[str, str]:
    path = Path(tmp)
    env = {
        "KLINE_DB_PATH": str(path / "candles.db"),
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

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, _test_env(tmp), clear=False), patch(
            "kronos_mvp.api.CandleStore", return_value=store
        ), patch("kronos_mvp.api.FundFactorStore", return_value=fund_store):
            client = TestClient(create_app())

            response = client.get("/api/data-freshness")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["updatedAt"], "2026-05-25T18:09:00+08:00")
        self.assertEqual(payload["klineUpdatedAt"], "2026-05-25T16:30:00+08:00")
        self.assertEqual(payload["fundUpdatedAt"], "2026-05-25T18:09:00+08:00")
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
                admin_client.post("/auth/login", json={"password": "secret-pass"})
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

                admin_login = admin_client.post("/auth/login", json={"password": "secret-pass"})
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
        ):
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
        ):
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
        ), patch("kronos_mvp.api.KronosPredictor", return_value=predictor):
            client = TestClient(create_app())
            _register(client)
            response = client.get("/api/predict/600519?horizon=1&paths=3&auto_sync=false")

        self.assertEqual(response.status_code, 200)
        analysis = response.json()["analysis"]
        self.assertEqual(analysis["horizon"], 1)
        self.assertEqual(analysis["signal"], "bullish")
        self.assertEqual(analysis["signalLabel"], "看涨")
        self.assertEqual(analysis["pathCount"], 1)
        self.assertEqual(analysis["upsideProbability"], 1.0)
        self.assertGreater(analysis["meanProjectedClose"], analysis["lastClose"])
        self.assertEqual(response.json()["billing"]["chargeType"], "free_credit")
        self.assertEqual(response.json()["billing"]["me"]["freeCreditsRemaining"], 9)

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

    def test_legacy_access_password_bootstraps_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, _test_env(tmp, APP_ACCESS_PASSWORD="secret-pass"), clear=False):
                client = TestClient(create_app())

                wrong = client.post("/auth/login", json={"password": "wrong"})
                correct = client.post("/auth/login", json={"password": "secret-pass"})
                users = client.get("/api/admin/users")

        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(correct.json()["user"]["username"], "admin")
        self.assertTrue(correct.json()["user"]["isAdmin"])
        self.assertEqual(users.status_code, 200)


if __name__ == "__main__":
    unittest.main()
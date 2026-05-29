from __future__ import annotations

from collections import deque
from contextlib import asynccontextmanager
import hashlib
import html
import hmac
import json
import logging
import os
from functools import lru_cache
from math import ceil
from threading import Lock
from time import monotonic
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel
from dotenv import load_dotenv

from .funds import (
    DEFAULT_FUND_HISTORY_DAYS,
    FundFactorStore,
    FundFactorSyncService,
    build_default_fund_providers,
    build_fund_analysis,
    latest_a_share_trade_date,
)
from .instruments import instrument_info, is_market_index_symbol, normalize_instrument_symbol
from .models import SyncResult
from .predictors import KronosPredictor
from .providers import ProviderError, build_default_providers, lookup_a_share_name
from .relative_strength import RelativeStrengthStore, RelativeStrengthSyncService, build_relative_strength_analysis
from .news import (
    DEFAULT_NEWS_ITEMS_LIMIT,
    DEFAULT_NEWS_LOOKBACK_DAYS,
    SHANGHAI_TZ,
    StockNewsStore,
    StockNewsSyncService,
    build_default_news_providers,
    build_news_sentiment_analysis,
)
from .storage import CandleStore
from .sync import DataSyncService
from .accounts import (
    ANNUAL_PRICE_CENTS,
    AccountError,
    AccountStore,
    cents_to_yuan,
    public_user,
    yuan_to_cents,
)
from .payments import PaymentError, SailaPayClient


load_dotenv()

ACCESS_COOKIE_NAME = "kronos_access"
SESSION_COOKIE_NAME = "kronos_session"
SUPPORTED_PAY_TYPES = {"alipay"}
DEFAULT_PREDICT_RATE_LIMIT_REQUESTS = 6
DEFAULT_PREDICT_RATE_LIMIT_WINDOW_SECONDS = 60
logger = logging.getLogger(__name__)


class PredictionRateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = max(0, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._requests: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> float | None:
        if self.limit <= 0:
            return None

        now = monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._requests.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return max(0.0, self.window_seconds - (now - bucket[0]))
            bucket.append(now)
        return None


class LoginRequest(BaseModel):
    username: str | None = None
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    contact: str | None = None


class PaymentOrderRequest(BaseModel):
    amountYuan: str
    orderType: str = "balance"
    payType: str | None = "alipay"


class AdminBalanceAdjustRequest(BaseModel):
    deltaYuan: str
    note: str = ""


class AdminFreeCreditsAdjustRequest(BaseModel):
    delta: int
    note: str = ""


class AdminAnnualRequest(BaseModel):
    days: int | None = 365


class PasswordChangeRequest(BaseModel):
    currentPassword: str
    newPassword: str


class AdminPasswordResetRequest(BaseModel):
    newPassword: str


def _fund_analysis_history_days() -> int:
    try:
        return max(1, int(os.getenv("FUND_ANALYSIS_LOOKBACK_DAYS", str(DEFAULT_FUND_HISTORY_DAYS))))
    except ValueError:
        return DEFAULT_FUND_HISTORY_DAYS


def _news_analysis_lookback_days() -> int:
    try:
        return max(1, int(os.getenv("NEWS_ANALYSIS_LOOKBACK_DAYS", str(DEFAULT_NEWS_LOOKBACK_DAYS))))
    except ValueError:
        return DEFAULT_NEWS_LOOKBACK_DAYS


def _news_analysis_items_limit() -> int:
    try:
        return max(1, int(os.getenv("NEWS_ANALYSIS_ITEMS_LIMIT", str(DEFAULT_NEWS_ITEMS_LIMIT))))
    except ValueError:
        return DEFAULT_NEWS_ITEMS_LIMIT


def _news_auto_sync_on_predict_miss() -> bool:
    value = os.getenv("NEWS_AUTO_SYNC_ON_PREDICT_MISS", "1")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _predict_rate_limit_requests() -> int:
    try:
        return max(0, int(os.getenv("PREDICT_RATE_LIMIT_REQUESTS", str(DEFAULT_PREDICT_RATE_LIMIT_REQUESTS))))
    except ValueError:
        return DEFAULT_PREDICT_RATE_LIMIT_REQUESTS


def _predict_rate_limit_window_seconds() -> int:
    try:
        return max(1, int(os.getenv("PREDICT_RATE_LIMIT_WINDOW_SECONDS", str(DEFAULT_PREDICT_RATE_LIMIT_WINDOW_SECONDS))))
    except ValueError:
        return DEFAULT_PREDICT_RATE_LIMIT_WINDOW_SECONDS


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if _should_prewarm_predictor_on_startup():
            logger.info("Prewarming Kronos predictor on startup.")
            _prewarm_predictor()
            logger.info("Kronos predictor prewarm completed.")
        yield

    app = FastAPI(title=os.getenv("APP_SITE_TITLE", "土豆A股预测研究"), version="0.1.0", lifespan=lifespan)
    _configure_cors(app)
    app.state.prediction_rate_limiter = PredictionRateLimiter(
        limit=_predict_rate_limit_requests(),
        window_seconds=_predict_rate_limit_window_seconds(),
    )
    store = CandleStore(os.getenv("KLINE_DB_PATH", "data/candles.db"))
    fund_store = FundFactorStore(os.getenv("FUND_DB_PATH", "data/fund_factors.db"))
    relative_store = RelativeStrengthStore(os.getenv("RELATIVE_DB_PATH", "data/relative_strength.db"))
    news_store = StockNewsStore(os.getenv("NEWS_DB_PATH", "data/news_sentiment.db"))
    account_store = AccountStore(_account_db_path(store))
    account_store.bootstrap_admin(os.getenv("ADMIN_USERNAME", "admin"), os.getenv("ADMIN_PASSWORD") or _access_password())
    payment_client = SailaPayClient()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _index_html()

    @app.get("/config.js", response_class=PlainTextResponse)
    def config_js() -> PlainTextResponse:
        payload = json.dumps(_client_config(), ensure_ascii=False)
        return PlainTextResponse(f"window.KRONOS_CONFIG = {payload};\n", media_type="application/javascript")

    @app.get("/api/data-freshness")
    def data_freshness() -> dict[str, object]:
        kline_updated_at = store.get_last_updated_at()
        fund_updated_at = fund_store.get_last_updated_at()
        relative_updated_at = relative_store.get_last_updated_at()
        news_updated_at = news_store.get_last_updated_at()
        latest_trade_date = fund_store.get_latest_trade_date()
        return {
            "updatedAt": _latest_timestamp(kline_updated_at, fund_updated_at, relative_updated_at, news_updated_at),
            "klineUpdatedAt": kline_updated_at,
            "fundUpdatedAt": fund_updated_at,
            "relativeUpdatedAt": relative_updated_at,
            "newsUpdatedAt": news_updated_at,
            "fundLatestTradeDate": latest_trade_date.isoformat() if latest_trade_date is not None else None,
        }

    @app.get("/auth/status")
    def auth_status(request: Request) -> dict[str, object]:
        user = _current_user(request, account_store)
        return {
            "protected": True,
            "authorized": user is not None,
            "user": public_user(user) if user is not None else None,
        }

    @app.post("/auth/login")
    def auth_login() -> dict[str, object]:
        raise HTTPException(status_code=410, detail="旧登录入口已停用，请改用 /api/auth/login。")

    @app.post("/api/auth/register")
    def register(payload: RegisterRequest, request: Request, response: Response) -> dict[str, object]:
        try:
            user = account_store.create_user(payload.username, payload.password, contact=payload.contact)
            _set_session_cookie(response, request, account_store.create_session(int(user["id"])))
            return {"user": public_user(user)}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/auth/login")
    def user_login(payload: LoginRequest, request: Request, response: Response) -> dict[str, object]:
        return _login_user(payload, request, response, account_store)

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, object]:
        account_store.revoke_session(request.cookies.get(SESSION_COOKIE_NAME, ""))
        response.delete_cookie(SESSION_COOKIE_NAME)
        response.delete_cookie(ACCESS_COOKIE_NAME)
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request) -> dict[str, object]:
        user = _require_current_user(request, account_store)
        return {"user": public_user(user)}

    @app.post("/api/me/change-password")
    def change_password(payload: PasswordChangeRequest, request: Request) -> dict[str, object]:
        user = _require_current_user(request, account_store)
        try:
            updated = account_store.change_password(int(user["id"]), payload.currentPassword, payload.newPassword)
            return {"user": public_user(updated)}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "backend": "kronos", "db": str(store.db_path)}

    @app.post("/api/sync/{symbol}")
    def sync_symbol(symbol: str, request: Request) -> dict[str, object]:
        _require_admin(request, account_store)
        service = DataSyncService(store=store, providers=build_default_providers())
        try:
            return service.sync_symbol(symbol).__dict__
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/predict/{symbol}")
    def predict_symbol(
        request: Request,
        symbol: str,
        horizon: int = 7,
        paths: int = 3,
        lookback: int = 512,
        auto_sync: bool = True,
    ) -> dict[str, object]:
        user = _require_current_user(request, account_store)
        _enforce_prediction_rate_limit(request, user)
        canonical_symbol = normalize_instrument_symbol(symbol)
        is_index = is_market_index_symbol(canonical_symbol)
        try:
            usage = account_store.authorize_prediction(int(user["id"]), canonical_symbol)
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        try:
            sync_info = {"attempted": False, "updated": False}
            relative_sync = {"attempted": False, "updated": False}
            if auto_sync:
                sync_info = _auto_sync_symbol(store, canonical_symbol)
                relative_sync = (
                    {"attempted": False, "updated": False, "skipped": True, "reason": "market_index"}
                    if is_index
                    else _auto_sync_relative_strength(relative_store, canonical_symbol)
                )

            candles = store.get_latest(canonical_symbol, limit=lookback)
            if len(candles) < 2:
                if sync_info.get("warning"):
                    raise HTTPException(
                        status_code=502,
                        detail=f"最新数据同步失败，且本地没有可用的K线缓存：{sync_info['warning']}",
                    )
                target_name = "指数" if is_index else "股票"
                raise HTTPException(status_code=404, detail=f"该{target_name}暂无本地K线数据，请先同步数据后再预测。")

            news_sync = {"attempted": False, "updated": False}
            if is_index:
                news_items = []
            else:
                news_items = news_store.get_latest(
                    canonical_symbol,
                    limit=_news_analysis_items_limit(),
                    max_age_days=_news_analysis_lookback_days(),
                )
                if not news_items and _news_auto_sync_on_predict_miss():
                    news_sync = _auto_sync_news(news_store, canonical_symbol)
                    news_items = news_store.get_latest(
                        canonical_symbol,
                        limit=_news_analysis_items_limit(),
                        max_age_days=_news_analysis_lookback_days(),
                    )

            predictor = _build_predictor()
            result = predictor.predict(canonical_symbol, candles, horizon=horizon, paths=paths)
            news_sentiment = build_news_sentiment_analysis(news_items)
            account_store.mark_prediction_succeeded(int(usage["id"]))
            fresh_user = account_store.get_user(int(user["id"]))
            symbol_name = lookup_a_share_name(canonical_symbol)
            analysis = _build_prediction_analysis(
                candles,
                result,
                horizon,
                symbol=canonical_symbol,
                relative_strength_store=None if is_index else relative_store,
                news_sentiment=news_sentiment,
            )
            if is_index:
                analysis["marketIndex"] = _build_market_index_analysis(candles)
            info = instrument_info(canonical_symbol, symbol_name)
            return {
                **result.to_dict(),
                "symbol": canonical_symbol,
                "symbolName": symbol_name,
                "instrument": info.to_dict(),
                "instrumentType": info.type,
                "history": [candle.to_dict() for candle in candles[-120:]],
                "analysis": analysis,
                "lookback": len(candles),
                "sync": {**sync_info, "relativeStrength": relative_sync, "news": news_sync},
                "billing": {**usage, "me": public_user(fresh_user) if fresh_user is not None else None},
            }
        except HTTPException as exc:
            account_store.mark_prediction_failed(int(usage["id"]), str(exc.detail))
            raise
        except RuntimeError as exc:
            account_store.mark_prediction_failed(int(usage["id"]), str(exc))
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/api/funds/{symbol}")
    def fund_analysis(symbol: str, request: Request, auto_sync: bool = False) -> dict[str, object]:
        _require_current_user(request, account_store)
        canonical_symbol = normalize_instrument_symbol(symbol)
        if is_market_index_symbol(canonical_symbol):
            raise HTTPException(status_code=400, detail="指数预测页不使用个股资金面和融资数据，请查看大盘结构分析。")
        sync_info = {"attempted": False, "updated": False}
        if auto_sync:
            sync_payload = _auto_sync_funds(fund_store)
            if isinstance(sync_payload, dict):
                sync_info = sync_payload
        factors = fund_store.get_latest(canonical_symbol, limit=_fund_analysis_history_days())
        if not factors and auto_sync and not sync_info.get("attempted"):
            sync_payload = _auto_sync_funds(fund_store, force=True)
            if isinstance(sync_payload, dict):
                sync_info = sync_payload
            factors = fund_store.get_latest(canonical_symbol, limit=_fund_analysis_history_days())
        if not factors:
            if sync_info.get("attempted") and sync_info.get("warning"):
                raise HTTPException(status_code=502, detail=f"资金面同步失败：{sync_info['warning']}")
            raise HTTPException(status_code=404, detail="该股票暂无资金面数据，请等待每日 18:09 同步后重试。")
        return {
            "symbol": canonical_symbol,
            "history": [factor.to_dict() for factor in factors],
            "analysis": build_fund_analysis(factors),
            "sync": sync_info,
        }

    @app.post("/api/payments/orders")
    def create_payment_order(payload: PaymentOrderRequest, request: Request) -> dict[str, object]:
        user = _require_current_user(request, account_store)
        if int(user["is_banned"]):
            raise HTTPException(status_code=403, detail="账号已被封禁，不能充值。")
        pay_type = payload.payType or "alipay"
        if pay_type not in SUPPORTED_PAY_TYPES:
            raise HTTPException(status_code=400, detail="当前仅支持支付宝充值。")
        try:
            payment_client.require_configured()
            amount_cents = ANNUAL_PRICE_CENTS if payload.orderType == "annual" else yuan_to_cents(payload.amountYuan)
            order = account_store.create_recharge_order(int(user["id"]), amount_cents, payload.orderType, pay_type)
            base_url = _public_base_url(request)
            pay_result = payment_client.create_payment(
                out_trade_no=order["out_trade_no"],
                name="预测包年服务" if payload.orderType == "annual" else f"预测余额充值 {cents_to_yuan(amount_cents)} 元",
                money=cents_to_yuan(amount_cents),
                notify_url=f"{base_url}/api/payments/notify",
                return_url=f"{base_url}/api/payments/return",
                client_ip=_client_ip(request),
                pay_type=pay_type,
                device=_request_device(request),
                param=f"user:{user['id']}:type:{payload.orderType}",
            )
            return {"order": _public_order(order), "payment": pay_result}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except PaymentError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.get("/api/payments/notify", response_class=PlainTextResponse)
    def payment_notify(request: Request) -> PlainTextResponse:
        params = dict(request.query_params)
        try:
            if not payment_client.verify(params):
                return PlainTextResponse("fail", status_code=400)
            if params.get("trade_status") != "TRADE_SUCCESS":
                return PlainTextResponse("success")
            amount_cents = yuan_to_cents(str(params.get("money", "0")))
            account_store.apply_paid_order(
                out_trade_no=str(params.get("out_trade_no", "")),
                trade_no=str(params.get("trade_no", "")),
                amount_cents=amount_cents,
                raw_notify=json.dumps(params, ensure_ascii=False),
            )
            return PlainTextResponse("success")
        except Exception:
            return PlainTextResponse("fail", status_code=400)

    @app.get("/api/payments/return", response_class=HTMLResponse)
    def payment_return(request: Request) -> str:
        home_url = f"{_public_base_url(request)}/?payment=return"
        return _payment_return_html(home_url)

    @app.post("/api/payments/convert-annual")
    def convert_annual(request: Request) -> dict[str, object]:
        user = _require_current_user(request, account_store)
        try:
            updated = account_store.convert_balance_to_annual(int(user["id"]))
            return {"user": public_user(updated)}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.get("/api/admin/users")
    def admin_users(request: Request) -> dict[str, object]:
        _require_admin(request, account_store)
        return {"users": account_store.list_users()}

    @app.post("/api/admin/users/{user_id}/adjust-balance")
    def admin_adjust_balance(user_id: int, payload: AdminBalanceAdjustRequest, request: Request) -> dict[str, object]:
        operator = _require_admin(request, account_store)
        try:
            updated = account_store.admin_adjust_balance(int(operator["id"]), user_id, yuan_to_cents(payload.deltaYuan), payload.note)
            return {"user": public_user(updated)}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/admin/users/{user_id}/adjust-free-credits")
    def admin_adjust_free_credits(user_id: int, payload: AdminFreeCreditsAdjustRequest, request: Request) -> dict[str, object]:
        operator = _require_admin(request, account_store)
        try:
            updated = account_store.admin_adjust_free_credits(int(operator["id"]), user_id, payload.delta, payload.note)
            return {"user": public_user(updated)}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/admin/users/{user_id}/ban")
    def admin_ban_user(user_id: int, request: Request) -> dict[str, object]:
        operator = _require_admin(request, account_store)
        try:
            updated = account_store.admin_set_banned(int(operator["id"]), user_id, True)
            return {"user": public_user(updated)}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/admin/users/{user_id}/unban")
    def admin_unban_user(user_id: int, request: Request) -> dict[str, object]:
        operator = _require_admin(request, account_store)
        try:
            updated = account_store.admin_set_banned(int(operator["id"]), user_id, False)
            return {"user": public_user(updated)}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/admin/users/{user_id}/set-annual")
    def admin_set_annual(user_id: int, payload: AdminAnnualRequest, request: Request) -> dict[str, object]:
        operator = _require_admin(request, account_store)
        try:
            updated = account_store.admin_set_annual_days(int(operator["id"]), user_id, payload.days)
            return {"user": public_user(updated)}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.post("/api/admin/users/{user_id}/reset-password")
    def admin_reset_password(user_id: int, payload: AdminPasswordResetRequest, request: Request) -> dict[str, object]:
        operator = _require_admin(request, account_store)
        try:
            updated = account_store.admin_reset_password(int(operator["id"]), user_id, payload.newPassword)
            return {"user": public_user(updated)}
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.get("/api/admin/orders")
    def admin_orders(request: Request) -> dict[str, object]:
        _require_admin(request, account_store)
        return {"orders": account_store.list_orders()}

    @app.get("/api/admin/usages")
    def admin_usages(request: Request) -> dict[str, object]:
        _require_admin(request, account_store)
        return {"usages": account_store.list_usages()}

    return app

def _index_html() -> str:
    return Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")


def _client_config() -> dict[str, str]:
    return {
        "apiBaseUrl": os.getenv("APP_API_BASE_URL", "").rstrip("/"),
        "siteTitle": os.getenv("APP_SITE_TITLE", "土豆A股预测研究"),
    }


def _payment_return_html(home_url: str) -> str:
        target = json.dumps(home_url, ensure_ascii=False)
        escaped_home_url = html.escape(home_url, quote=True)
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>支付结果确认中</title>
    <meta http-equiv="refresh" content="2;url={escaped_home_url}">
    <style>
        body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            color: #182231;
            background: linear-gradient(180deg, #f8f4e6, #eef2ea);
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }}
        main {{
            width: min(520px, calc(100vw - 32px));
            padding: 30px;
            border: 1px solid rgba(24, 34, 49, 0.1);
            border-radius: 24px;
            background: rgba(255, 255, 255, 0.92);
            box-shadow: 0 24px 56px rgba(17, 24, 39, 0.08);
            text-align: center;
        }}
        h1 {{ margin: 0; font-size: 26px; }}
        p {{ margin: 14px 0 0; color: #667587; line-height: 1.8; }}
        a {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            height: 46px;
            margin-top: 22px;
            padding: 0 22px;
            border-radius: 999px;
            background: #17324d;
            color: #fff;
            font-weight: 700;
            text-decoration: none;
        }}
    </style>
</head>
<body>
    <main>
        <h1>支付结果正在确认中</h1>
        <p>正在自动返回登录后的首页，请稍后查看余额或包年状态。</p>
        <a href="{escaped_home_url}">立即返回首页</a>
    </main>
    <script>
        window.setTimeout(() => window.location.replace({target}), 1200);
    </script>
</body>
</html>"""


def _account_db_path(store: CandleStore) -> str:
    configured = os.getenv("APP_DB_PATH")
    if configured:
        return configured
    return str(Path(store.db_path).with_name("app.db"))


def _current_user(request: Request, account_store: AccountStore):
    return account_store.get_user_by_session(request.cookies.get(SESSION_COOKIE_NAME))


def _require_current_user(request: Request, account_store: AccountStore):
    user = _current_user(request, account_store)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录。")
    return user


def _require_admin(request: Request, account_store: AccountStore):
    user = _require_current_user(request, account_store)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限。")
    return user


def _enforce_prediction_rate_limit(request: Request, user: Any) -> None:
    limiter = getattr(request.app.state, "prediction_rate_limiter", None)
    if limiter is None:
        return

    retry_after = limiter.check(f"user:{int(user['id'])}")
    if retry_after is None:
        return

    retry_seconds = max(1, ceil(retry_after))
    raise HTTPException(
        status_code=429,
        detail=f"预测请求过于频繁，请在 {retry_seconds} 秒后重试。",
        headers={"Retry-After": str(retry_seconds)},
    )


def _login_user(
    payload: LoginRequest,
    request: Request,
    response: Response,
    account_store: AccountStore,
) -> dict[str, object]:
    if not payload.username:
        raise HTTPException(status_code=400, detail="请输入用户名。")
    try:
        user = account_store.authenticate_user(payload.username, payload.password)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误。")
    _set_session_cookie(response, request, account_store.create_session(int(user["id"])))
    return {"user": public_user(user)}


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite=os.getenv("APP_COOKIE_SAMESITE", "lax"),
        secure=_request_is_https(request),
        max_age=7 * 24 * 60 * 60,
    )


def _public_base_url(request: Request) -> str:
    configured = os.getenv("APP_PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    proto = forwarded_proto or request.url.scheme
    return f"{proto}://{request.headers.get('host', request.url.netloc)}".rstrip("/")


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client else "127.0.0.1"


def _request_device(request: Request) -> str:
    user_agent = request.headers.get("user-agent", "").lower()
    return "mobile" if any(token in user_agent for token in ["mobile", "android", "iphone"]) else "pc"


def _public_order(row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "outTradeNo": row["out_trade_no"],
        "orderType": row["order_type"],
        "payType": row["pay_type"],
        "amountCents": int(row["amount_cents"]),
        "amountYuan": cents_to_yuan(int(row["amount_cents"])),
        "status": row["status"],
        "createdAt": row["created_at"],
        "paidAt": row["paid_at"],
    }


@lru_cache(maxsize=8)
def _cached_predictor(model_name: str, tokenizer_name: str, device: str, predictor_cls_id: int) -> KronosPredictor:
    return KronosPredictor(
        model_name=model_name,
        tokenizer_name=tokenizer_name,
        device=device,
    )


def _build_predictor() -> KronosPredictor:
    return _cached_predictor(
        os.getenv("KRONOS_MODEL", "NeoQuasar/Kronos-small"),
        os.getenv("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base"),
        os.getenv("KRONOS_DEVICE", "cpu"),
        id(KronosPredictor),
    )


def _should_prewarm_predictor_on_startup() -> bool:
    value = os.getenv("KRONOS_PREWARM_ON_STARTUP", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _prewarm_predictor() -> KronosPredictor:
    predictor = _build_predictor()
    try:
        predictor._get_upstream_predictor()
    except Exception:
        _cached_predictor.cache_clear()
        raise
    return predictor


def _access_password() -> str:
    return os.getenv("APP_ACCESS_PASSWORD", "")


def _is_access_protected() -> bool:
    return bool(_access_password())


def _access_cookie_value() -> str:
    secret = _access_password().encode("utf-8")
    return hmac.new(secret, b"kronos-access", hashlib.sha256).hexdigest()


def _is_valid_password(candidate: str) -> bool:
    secret = _access_password()
    if not secret:
        return True
    return hmac.compare_digest(candidate, secret)


def _is_request_authorized(request: Request) -> bool:
    if not _is_access_protected():
        return True
    token = request.cookies.get(ACCESS_COOKIE_NAME, "")
    if not token:
        return False
    return hmac.compare_digest(token, _access_cookie_value())


def _require_authorized_request(request: Request) -> None:
    if _is_request_authorized(request):
        return
    raise HTTPException(status_code=401, detail="password required")


def _request_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _build_prediction_analysis(
    candles: list[Any],
    result,
    horizon: int,
    symbol: str | None = None,
    relative_strength_store: RelativeStrengthStore | None = None,
    news_sentiment: dict[str, object] | None = None,
) -> dict[str, object]:
    effective_news_sentiment = news_sentiment or build_news_sentiment_analysis([])
    if not candles:
        blended_probability = _blend_upside_probability(0.0, effective_news_sentiment)
        blended_signal, blended_signal_label = _signal_from_probability(blended_probability)
        return {
            "horizon": int(horizon),
            "lastDate": None,
            "lastClose": 0.0,
            "pathCount": 0,
            "signal": "neutral",
            "signalLabel": "震荡",
            "confidence": 0.0,
            "upsideProbability": 0.0,
            "downsideProbability": 0.0,
            "flatProbability": 1.0,
            "volatilityAmplificationProbability": 0.0,
            "meanProjectedClose": 0.0,
            "meanProjectedReturn": 0.0,
            "projectedCloseLow": 0.0,
            "projectedCloseHigh": 0.0,
            "signalBlended": blended_signal,
            "signalBlendedLabel": blended_signal_label,
            "upsideProbabilityBlended": blended_probability,
            "priceVolumeConfirmation": _empty_price_volume_confirmation(),
            "relativeStrength": build_relative_strength_analysis(symbol or result.symbol, [], relative_strength_store),
            "newsSentiment": effective_news_sentiment,
        }

    last_candle = candles[-1]
    last_close = float(last_candle.close)
    price_volume_confirmation = _build_price_volume_confirmation(candles)
    relative_strength = build_relative_strength_analysis(symbol or result.symbol, candles, relative_strength_store)

    end_closes: list[float] = []
    projected_volatilities: list[float] = []
    for path in result.paths:
        closes = [float(point.close) for point in path.points]
        if not closes:
            continue
        end_closes.append(closes[-1])
        projected_volatilities.append(_mean_abs_return([last_close, *closes]))

    if not end_closes:
        blended_probability = _blend_upside_probability(0.0, effective_news_sentiment)
        blended_signal, blended_signal_label = _signal_from_probability(blended_probability)
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
            "signalBlended": blended_signal,
            "signalBlendedLabel": blended_signal_label,
            "upsideProbabilityBlended": blended_probability,
            "priceVolumeConfirmation": price_volume_confirmation,
            "relativeStrength": relative_strength,
            "newsSentiment": effective_news_sentiment,
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
    blended_probability = _blend_upside_probability(upside_probability, effective_news_sentiment)
    blended_signal, blended_signal_label = _signal_from_probability(blended_probability)
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
        "signalBlended": blended_signal,
        "signalBlendedLabel": blended_signal_label,
        "upsideProbabilityBlended": blended_probability,
        "priceVolumeConfirmation": price_volume_confirmation,
        "relativeStrength": relative_strength,
        "newsSentiment": effective_news_sentiment,
    }


def _build_market_index_analysis(candles: list[Any]) -> dict[str, object]:
    if len(candles) < 2:
        return {
            "available": False,
            "score": 50,
            "signal": "neutral",
            "signalLabel": "大盘待确认",
            "summary": "大盘结构样本不足，暂不做指数层修正。",
            "detail": "上证指数K线样本不足。",
            "components": [],
            "metrics": {},
        }

    closes = [float(candle.close) for candle in candles]
    highs = [float(candle.high) for candle in candles]
    lows = [float(candle.low) for candle in candles]
    volumes = [float(getattr(candle, "volume", 0.0) or 0.0) for candle in candles]
    amounts = [_optional_metric(getattr(candle, "amount", None)) for candle in candles]
    last_close = closes[-1]
    previous_close = closes[-2]
    ma5 = _window_average(closes, 5)
    ma10 = _window_average(closes, 10)
    ma20 = _window_average(closes, 20)
    ma60 = _window_average(closes, 60)
    return_5d = _window_return(closes, 5)
    return_20d = _window_return(closes, 20)
    amount_ratio5 = _series_ratio(amounts, 5)
    amount_ratio20 = _series_ratio(amounts, 20)
    volume_ratio5 = _series_ratio(volumes, 5)
    recent_high = max(highs[-20:]) if len(highs) >= 20 else max(highs)
    recent_low = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    close_position20 = _close_position(recent_low, recent_high, last_close)
    volatility20 = _mean_abs_return(closes[-21:]) if len(closes) >= 21 else _mean_abs_return(closes)

    components = [
        _market_index_trend_component(last_close, previous_close, ma5, ma10, ma20, ma60),
        _market_index_momentum_component(return_5d, return_20d),
        _market_index_amount_component(last_close, previous_close, amount_ratio5, amount_ratio20, volume_ratio5),
        _market_index_structure_component(close_position20, volatility20),
    ]
    score = int(round(sum(int(component["score"]) for component in components) / (len(components) * 25) * 100))
    signal, signal_label = _signal_from_market_index_score(score)
    metrics = {
        "lastClose": last_close,
        "previousClose": previous_close,
        "return1d": (last_close - previous_close) / previous_close if previous_close > 0 else None,
        "return5d": return_5d,
        "return20d": return_20d,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "ma5Distance": _distance_to_average(last_close, ma5),
        "ma20Distance": _distance_to_average(last_close, ma20),
        "amountRatio5": amount_ratio5,
        "amountRatio20": amount_ratio20,
        "volumeRatio5": volume_ratio5,
        "closePosition20": close_position20,
        "volatility20": volatility20,
        "latestAmount": amounts[-1],
        "latestVolume": volumes[-1],
    }
    summary = _build_market_index_summary(signal_label, metrics)
    return {
        "available": True,
        "score": score,
        "signal": signal,
        "signalLabel": signal_label,
        "summary": summary,
        "detail": summary,
        "components": components,
        "metrics": metrics,
    }


def _window_return(values: list[float], window: int) -> float | None:
    if len(values) <= window or window <= 0:
        return None
    start = values[-window - 1]
    end = values[-1]
    if start <= 0:
        return None
    return (end - start) / start


def _distance_to_average(value: float, average_value: float | None) -> float | None:
    if average_value is None or average_value <= 0:
        return None
    return (value - average_value) / average_value


def _market_index_trend_component(
    last_close: float,
    previous_close: float,
    ma5: float | None,
    ma10: float | None,
    ma20: float | None,
    ma60: float | None,
) -> dict[str, object]:
    averages = [value for value in (ma5, ma10, ma20, ma60) if value is not None]
    if not averages:
        score = 18 if last_close >= previous_close else 6
        verdict = "指数高于前收" if last_close >= previous_close else "指数低于前收"
        return {"label": "指数趋势位置", "score": score, "verdict": verdict, "detail": f"{verdict}。"}
    above_count = sum(1 for value in averages if last_close >= value)
    if above_count == len(averages):
        score, verdict = 25, "站上可用均线"
    elif ma5 is not None and ma20 is not None and last_close >= ma5 >= ma20:
        score, verdict = 20, "短期均线偏强"
    elif above_count >= max(1, len(averages) // 2):
        score, verdict = 14, "均线结构分化"
    elif last_close >= previous_close:
        score, verdict = 8, "低位修复中"
    else:
        score, verdict = 0, "仍受均线压制"
    return {"label": "指数趋势位置", "score": score, "verdict": verdict, "detail": f"{verdict}。"}


def _market_index_momentum_component(return_5d: float | None, return_20d: float | None) -> dict[str, object]:
    available = [value for value in (return_5d, return_20d) if value is not None]
    if not available:
        return {"label": "指数动量", "score": 12, "verdict": "样本不足", "detail": "5日和20日指数动量样本不足。"}
    positive = sum(1 for value in available if value > 0)
    if positive == len(available):
        score, verdict = 25 if len(available) >= 2 else 18, "多周期回升"
    elif return_5d is not None and return_5d > 0:
        score, verdict = 16, "短线回暖"
    elif all(value < 0 for value in available):
        score, verdict = 0 if len(available) >= 2 else 6, "多周期走弱"
    else:
        score, verdict = 10, "动量分化"
    return {"label": "指数动量", "score": score, "verdict": verdict, "detail": f"{verdict}。"}


def _market_index_amount_component(
    last_close: float,
    previous_close: float,
    amount_ratio5: float | None,
    amount_ratio20: float | None,
    volume_ratio5: float | None,
) -> dict[str, object]:
    reference = amount_ratio5 if amount_ratio5 is not None else amount_ratio20
    if reference is None and volume_ratio5 is None:
        return {"label": "成交额确认", "score": 12, "verdict": "量能样本不足", "detail": "指数成交额和成交量样本不足。"}
    ratio = reference if reference is not None else volume_ratio5
    rising = last_close >= previous_close
    if ratio is not None and ratio >= 1.12 and rising:
        score, verdict = 25, "放量上行"
    elif ratio is not None and ratio >= 1.0 and rising:
        score, verdict = 18, "量价配合"
    elif ratio is not None and ratio < 0.85 and not rising:
        score, verdict = 0, "缩量下行"
    elif not rising:
        score, verdict = 6, "量价偏弱"
    else:
        score, verdict = 12, "成交基本均衡"
    return {"label": "成交额确认", "score": score, "verdict": verdict, "detail": f"{verdict}。"}


def _market_index_structure_component(close_position20: float, volatility20: float) -> dict[str, object]:
    if close_position20 >= 0.72 and volatility20 <= 0.018:
        score, verdict = 25, "收盘靠近区间上沿且波动可控"
    elif close_position20 >= 0.58:
        score, verdict = 18, "收盘位于区间偏强位置"
    elif close_position20 >= 0.38:
        score, verdict = 12, "收盘仍在震荡中段"
    elif close_position20 >= 0.22:
        score, verdict = 6, "收盘靠近区间下沿"
    else:
        score, verdict = 0, "收盘接近近期低位"
    return {"label": "区间与波动", "score": score, "verdict": verdict, "detail": f"{verdict}。"}


def _signal_from_market_index_score(score: int) -> tuple[str, str]:
    if score >= 65:
        return "bullish", "大盘偏强"
    if score <= 35:
        return "bearish", "大盘偏弱"
    return "neutral", "大盘震荡"


def _build_market_index_summary(signal_label: str, metrics: dict[str, object]) -> str:
    return_5d = metrics.get("return5d")
    return_20d = metrics.get("return20d")
    ma20_distance = metrics.get("ma20Distance")
    amount_ratio = metrics.get("amountRatio5") or metrics.get("amountRatio20")
    close_position = metrics.get("closePosition20")
    parts = [f"大盘结构：{signal_label}"]
    if isinstance(return_5d, (int, float)):
        parts.append(f"近5日涨跌幅{return_5d * 100:+.2f}%")
    if isinstance(return_20d, (int, float)):
        parts.append(f"近20日涨跌幅{return_20d * 100:+.2f}%")
    if isinstance(ma20_distance, (int, float)):
        parts.append(f"相对20日均线{ma20_distance * 100:+.2f}%")
    if isinstance(amount_ratio, (int, float)):
        parts.append(f"成交额约为均值的{amount_ratio:.2f}倍")
    if isinstance(close_position, (int, float)):
        parts.append(f"收盘位于近20日区间{close_position * 100:.0f}%位置")
    return "；".join(parts) + "。"


def _build_price_volume_confirmation(candles: list) -> dict[str, object]:
    last_candle = candles[-1]
    last_open = float(last_candle.open)
    last_high = float(last_candle.high)
    last_low = float(last_candle.low)
    last_close = float(last_candle.close)
    last_volume = float(last_candle.volume or 0.0)
    last_amount = _optional_metric(getattr(last_candle, "amount", None))
    last_turnover = _optional_metric(getattr(last_candle, "turnover", None))

    closes = [float(candle.close) for candle in candles]
    highs = [float(candle.high) for candle in candles]
    lows = [float(candle.low) for candle in candles]
    volumes = [float(getattr(candle, "volume", 0.0) or 0.0) for candle in candles]
    amounts = [_optional_metric(getattr(candle, "amount", None)) for candle in candles]
    turnovers = [_optional_metric(getattr(candle, "turnover", None)) for candle in candles]

    previous_close = closes[-2] if len(closes) >= 2 else None
    previous_high = max(highs[:-1]) if len(highs) >= 2 else None
    previous_low = min(lows[:-1]) if len(lows) >= 2 else None
    ma5 = _window_average(closes, 5)
    ma10 = _window_average(closes, 10)
    ma20 = _window_average(closes, 20)
    volume_ratio5 = _volume_ratio(volumes, 5)
    volume_ratio10 = _volume_ratio(volumes, 10)
    amount_ratio5 = _series_ratio(amounts, 5)
    amount_ratio10 = _series_ratio(amounts, 10)
    turnover_ratio5 = _series_ratio(turnovers, 5)
    turnover_ratio10 = _series_ratio(turnovers, 10)
    close_position = _close_position(last_low, last_high, last_close)
    body_ratio = _body_ratio(last_open, last_close, last_low, last_high)

    trend_component = _price_trend_component(
        last_close,
        previous_close,
        {"ma5": ma5, "ma10": ma10, "ma20": ma20},
    )
    breakout_component = _price_breakout_component(last_close, previous_high, previous_low)
    volume_component = _price_volume_component(
        last_open,
        last_close,
        last_volume,
        last_amount,
        last_turnover,
        volume_ratio5,
        volume_ratio10,
        amount_ratio5,
        amount_ratio10,
        turnover_ratio5,
        turnover_ratio10,
    )
    structure_component = _candle_structure_component(last_open, last_close, last_low, last_high)
    components = [trend_component, breakout_component, volume_component, structure_component]

    score = int(round(sum(int(component["score"]) for component in components) / (len(components) * 25) * 100))
    signal, signal_label = _signal_from_confirmation_score(score)
    detail = _build_price_volume_detail(
        signal_label,
        close_position,
        volume_ratio5,
        volume_ratio10,
        amount_ratio5,
        amount_ratio10,
        last_turnover,
        turnover_ratio5,
        turnover_ratio10,
        trend_component,
        breakout_component,
        volume_component,
        structure_component,
    )

    return {
        "score": score,
        "signal": signal,
        "signalLabel": signal_label,
        "detail": detail,
        "components": components,
        "metrics": {
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "volumeRatio5": volume_ratio5,
            "volumeRatio10": volume_ratio10,
            "amountRatio5": amount_ratio5,
            "amountRatio10": amount_ratio10,
            "turnoverRate": last_turnover,
            "turnoverRatio5": turnover_ratio5,
            "turnoverRatio10": turnover_ratio10,
            "closePosition": close_position,
            "bodyRatio": body_ratio,
            "breakoutHigh": previous_high,
            "breakdownLow": previous_low,
            "isBreakout": previous_high is not None and last_close > previous_high,
            "isBreakdown": previous_low is not None and last_close < previous_low,
            "lastVolume": last_volume,
            "lastAmount": last_amount,
        },
    }


def _empty_price_volume_confirmation() -> dict[str, object]:
    return {
        "score": 50,
        "signal": "neutral",
        "signalLabel": "价量中性",
        "detail": "价量确认：样本不足。当前缺少可用K线数据，暂不放大方向判断。",
        "components": [],
        "metrics": {},
    }


def _price_trend_component(
    last_close: float,
    previous_close: float | None,
    moving_averages: dict[str, float | None],
) -> dict[str, object]:
    available = [(name, value) for name, value in moving_averages.items() if value is not None]
    if available:
        above = [name for name, value in available if last_close >= float(value)]
        if len(above) == len(available):
            score, verdict = 25, "收盘站上可用均线"
        elif len(above) >= max(1, len(available) - 1):
            score, verdict = 18, "收盘大体站上短期均线"
        elif above:
            score, verdict = 12, "均线方向分化"
        elif previous_close is not None and last_close >= previous_close:
            score, verdict = 8, "价格回升但仍受均线压制"
        else:
            score, verdict = 0, "收盘仍压在均线下方"
        return {
            "label": "均线位置",
            "score": score,
            "verdict": verdict,
            "detail": f"{verdict}。",
        }

    if previous_close is None:
        return {
            "label": "均线位置",
            "score": 12,
            "verdict": "样本不足",
            "detail": "均线样本不足，先不据此放大结论。",
        }

    if last_close > previous_close:
        score, verdict = 18, "最新收盘高于前收"
    elif last_close < previous_close:
        score, verdict = 6, "最新收盘低于前收"
    else:
        score, verdict = 12, "最新收盘与前收持平"
    return {
        "label": "均线位置",
        "score": score,
        "verdict": verdict,
        "detail": f"{verdict}。",
    }


def _price_breakout_component(last_close: float, previous_high: float | None, previous_low: float | None) -> dict[str, object]:
    if previous_high is None or previous_low is None:
        return {
            "label": "区间突破",
            "score": 12,
            "verdict": "样本不足",
            "detail": "近期区间样本不足，暂不放大突破判断。",
        }

    if last_close > previous_high:
        score, verdict = 25, "收盘突破近期高位"
    elif last_close >= previous_high * 0.985:
        score, verdict = 18, "收盘接近近期高位"
    elif last_close < previous_low:
        score, verdict = 0, "收盘跌破近期低位"
    elif last_close <= previous_low * 1.015:
        score, verdict = 6, "收盘靠近近期低位"
    else:
        score, verdict = 12, "收盘仍在近期区间内"

    return {
        "label": "区间突破",
        "score": score,
        "verdict": verdict,
        "detail": f"{verdict}。",
    }


def _price_volume_component(
    last_open: float,
    last_close: float,
    last_volume: float,
    last_amount: float | None,
    last_turnover: float | None,
    volume_ratio5: float | None,
    volume_ratio10: float | None,
    amount_ratio5: float | None,
    amount_ratio10: float | None,
    turnover_ratio5: float | None,
    turnover_ratio10: float | None,
) -> dict[str, object]:
    volume_reference = volume_ratio5 if volume_ratio5 is not None else volume_ratio10
    amount_reference = amount_ratio5 if amount_ratio5 is not None else amount_ratio10
    turnover_reference = turnover_ratio5 if turnover_ratio5 is not None else turnover_ratio10
    available_references = [value for value in (volume_reference, amount_reference, turnover_reference) if value is not None]
    if not available_references and last_volume <= 0 and last_amount is None and last_turnover is None:
        return {
            "label": "量能确认",
            "score": 12,
            "verdict": "量能样本不足",
            "detail": "成交量样本不足，暂不据此强化方向。",
        }

    positive_signals = 0
    negative_signals = 0
    for value, strong_threshold, weak_threshold in (
        (volume_reference, 1.15, 0.85),
        (amount_reference, 1.12, 0.88),
        (turnover_reference, 1.05, 0.85),
    ):
        if value is None:
            continue
        if value >= strong_threshold:
            positive_signals += 1
        elif value < weak_threshold:
            negative_signals += 1

    if last_close >= last_open and positive_signals >= 2 and negative_signals == 0:
        score, verdict = 25, "量价换手齐升"
    elif last_close >= last_open and positive_signals >= 1 and negative_signals == 0:
        score, verdict = 18, "量价配合偏强"
    elif negative_signals >= 2 and last_close < last_open:
        score, verdict = 0, "量价走弱"
    elif negative_signals >= 2:
        score, verdict = 6, "量价背离"
    elif negative_signals >= 1:
        score, verdict = 6, "量能偏弱"
    else:
        score, verdict = 12, "量能基本持平"

    return {
        "label": "量能确认",
        "score": score,
        "verdict": verdict,
        "detail": f"{verdict}。",
    }


def _candle_structure_component(last_open: float, last_close: float, last_low: float, last_high: float) -> dict[str, object]:
    close_position = _close_position(last_low, last_high, last_close)
    body_ratio = _body_ratio(last_open, last_close, last_low, last_high)
    if close_position >= 0.72 and last_close >= last_open and body_ratio >= 0.35:
        score, verdict = 25, "收盘贴近当日高位"
    elif close_position >= 0.58 and last_close >= last_open:
        score, verdict = 18, "收盘位于日内强势区"
    elif close_position >= 0.42:
        score, verdict = 12, "收盘位于日内中位"
    elif close_position >= 0.25:
        score, verdict = 6, "收盘偏向日内下沿"
    else:
        score, verdict = 0, "收盘接近日内低位"

    return {
        "label": "收盘质量",
        "score": score,
        "verdict": verdict,
        "detail": f"{verdict}。",
    }


def _build_price_volume_detail(
    signal_label: str,
    close_position: float,
    volume_ratio5: float | None,
    volume_ratio10: float | None,
    amount_ratio5: float | None,
    amount_ratio10: float | None,
    last_turnover: float | None,
    turnover_ratio5: float | None,
    turnover_ratio10: float | None,
    trend_component: dict[str, object],
    breakout_component: dict[str, object],
    volume_component: dict[str, object],
    structure_component: dict[str, object],
) -> str:
    if volume_ratio5 is not None:
        volume_text = f"量能约为近 5 日均量的 {volume_ratio5:.2f} 倍"
    elif volume_ratio10 is not None:
        volume_text = f"量能约为近 10 日均量的 {volume_ratio10:.2f} 倍"
    else:
        volume_text = "量能样本暂不足"

    if amount_ratio5 is not None:
        amount_text = f"成交额约为近 5 日均额的 {amount_ratio5:.2f} 倍"
    elif amount_ratio10 is not None:
        amount_text = f"成交额约为近 10 日均额的 {amount_ratio10:.2f} 倍"
    else:
        amount_text = "成交额样本暂不足"

    if turnover_ratio5 is not None:
        turnover_text = f"换手率约为近 5 日均换手的 {turnover_ratio5:.2f} 倍"
    elif turnover_ratio10 is not None:
        turnover_text = f"换手率约为近 10 日均换手的 {turnover_ratio10:.2f} 倍"
    elif last_turnover is not None:
        turnover_text = f"最新换手率 {last_turnover:.2f}%"
    else:
        turnover_text = "换手率样本暂不足"

    return (
        f"价量确认：{signal_label}。"
        f"{trend_component['verdict']}；{breakout_component['verdict']}；{volume_component['verdict']}，{volume_text}；"
        f"{amount_text}；{turnover_text}；"
        f"{structure_component['verdict']}，收盘位于当日振幅的 {close_position * 100:.0f}% 附近。"
    )


def _window_average(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    sample = values[-window:]
    return sum(sample) / len(sample)


def _volume_ratio(volumes: list[float], window: int) -> float | None:
    return _series_ratio([float(value) for value in volumes], window)


def _series_ratio(values: list[float | None], window: int) -> float | None:
    if len(values) <= 1 or window <= 0:
        return None
    current = values[-1]
    if current is None or current <= 0:
        return None
    previous_values = values[:-1]
    sample = [value for value in previous_values[-window:] if value is not None and value > 0]
    if not sample:
        return None
    baseline = sum(sample) / len(sample)
    if baseline <= 0:
        return None
    return current / baseline


def _optional_metric(value: object) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if numeric == numeric else None


def _close_position(low: float, high: float, close: float) -> float:
    span = high - low
    if span <= 0:
        return 0.5
    return max(0.0, min(1.0, (close - low) / span))


def _body_ratio(open_price: float, close_price: float, low: float, high: float) -> float:
    span = high - low
    if span <= 0:
        return 0.0
    return abs(close_price - open_price) / span


def _signal_from_confirmation_score(score: int) -> tuple[str, str]:
    if score >= 65:
        return "bullish", "价量偏强"
    if score <= 35:
        return "bearish", "价量偏弱"
    return "neutral", "价量中性"


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


def _auto_sync_relative_strength(store: RelativeStrengthStore, symbol: str) -> dict[str, object]:
    if is_market_index_symbol(symbol):
        return {"attempted": False, "updated": False, "skipped": True, "reason": "market_index"}
    service = RelativeStrengthSyncService(store=store)
    try:
        result = service.sync_symbol(symbol)
        return {
            "attempted": True,
            "updated": bool(result.rows or result.mapping_rows),
            **result.to_dict(),
        }
    except ProviderError as exc:
        return {
            "attempted": True,
            "updated": False,
            "warning": str(exc),
        }


def _auto_sync_funds(store: FundFactorStore, force: bool = False) -> dict[str, object]:
    try:
        target_trade_date = latest_a_share_trade_date()
    except ProviderError as exc:
        return {
            "attempted": True,
            "updated": False,
            "warning": str(exc),
        }

    current_trade_date = store.get_latest_trade_date()
    if not force and current_trade_date is not None and current_trade_date >= target_trade_date:
        return {
            "attempted": False,
            "updated": False,
            "latestTradeDate": current_trade_date.isoformat(),
        }

    service = FundFactorSyncService(store=store, providers=build_default_fund_providers())
    try:
        result = service.sync_recent(history_days=_fund_analysis_history_days(), trade_date=target_trade_date)
        return {
            "attempted": True,
            "updated": bool(result.synced_trade_dates),
            **result.to_dict(),
        }
    except ProviderError as exc:
        return {
            "attempted": True,
            "updated": False,
            "warning": str(exc),
        }


def _auto_sync_news(store: StockNewsStore, symbol: str, force: bool = False) -> dict[str, object]:
    if is_market_index_symbol(symbol):
        return {"attempted": False, "updated": False, "skipped": True, "reason": "market_index"}
    last_updated_at = store.get_symbol_last_updated_at(symbol)
    if not force and last_updated_at:
        normalized = last_updated_at[:-1] + "+00:00" if last_updated_at.endswith("Z") else last_updated_at
        try:
            last_moment = datetime.fromisoformat(normalized)
            if last_moment.tzinfo is None:
                last_moment = last_moment.replace(tzinfo=SHANGHAI_TZ)
            if datetime.now(SHANGHAI_TZ) - last_moment.astimezone(SHANGHAI_TZ) < timedelta(hours=12):
                return {
                    "attempted": False,
                    "updated": False,
                    "latestUpdatedAt": last_updated_at,
                }
        except ValueError:
            pass

    service = StockNewsSyncService(store=store, providers=build_default_news_providers())
    try:
        result = service.sync_symbol(symbol)
        if result.rows <= 0:
            return {
                "attempted": True,
                "updated": False,
                **result.to_dict(),
                "warning": "新闻数据源未返回可用新闻，可能是上游接口空结果或该股票暂缺新闻。",
            }
        return {
            "attempted": True,
            "updated": bool(result.rows > 0),
            **result.to_dict(),
        }
    except ProviderError as exc:
        return {
            "attempted": True,
            "updated": False,
            "warning": str(exc),
        }


def _latest_timestamp(*values: str | None) -> str | None:
    latest_value: str | None = None
    latest_moment: datetime | None = None
    for value in values:
        if not value:
            continue
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            moment = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if latest_moment is None or moment > latest_moment:
            latest_moment = moment
            latest_value = value
    return latest_value


def _mean_abs_return(values: list[float]) -> float:
    returns: list[float] = []
    for prev, current in zip(values, values[1:]):
        if prev > 0:
            returns.append(abs((current - prev) / prev))
    return sum(returns) / len(returns) if returns else 0.0


def _blend_upside_probability(base_probability: float, news_sentiment: dict[str, object]) -> float:
    news_score = float(news_sentiment.get("score", 50) or 50)
    sentiment_bias = (news_score - 50.0) / 50.0
    blended = base_probability * 0.82 + (0.5 + sentiment_bias * 0.5) * 0.18
    return max(0.0, min(1.0, blended))


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
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["*"],
    )

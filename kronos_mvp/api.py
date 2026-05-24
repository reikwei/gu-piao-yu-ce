from __future__ import annotations

import hashlib
import html
import hmac
import json
import os
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
from .models import SyncResult
from .predictors import KronosPredictor
from .providers import ProviderError, build_default_providers
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


def create_app() -> FastAPI:
    app = FastAPI(title=os.getenv("APP_SITE_TITLE", "土豆A股预测研究"), version="0.1.0")
    _configure_cors(app)
    store = CandleStore(os.getenv("KLINE_DB_PATH", "data/candles.db"))
    fund_store = FundFactorStore(os.getenv("FUND_DB_PATH", "data/fund_factors.db"))
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

    @app.get("/auth/status")
    def auth_status(request: Request) -> dict[str, object]:
        user = _current_user(request, account_store)
        return {
            "protected": True,
            "authorized": user is not None,
            "user": public_user(user) if user is not None else None,
        }

    @app.post("/auth/login")
    def auth_login(payload: LoginRequest, request: Request, response: Response) -> dict[str, object]:
        username = payload.username or "admin"
        try:
            user = account_store.authenticate_user(username, payload.password)
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if user is None:
            raise HTTPException(status_code=401, detail="invalid password")
        _set_session_cookie(response, request, account_store.create_session(int(user["id"])))
        return {"protected": True, "authorized": True, "user": public_user(user)}

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
        try:
            usage = account_store.authorize_prediction(int(user["id"]), symbol)
        except AccountError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        try:
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
            predictor = _build_predictor()
            result = predictor.predict(symbol, candles, horizon=horizon, paths=paths)
            account_store.mark_prediction_succeeded(int(usage["id"]))
            fresh_user = account_store.get_user(int(user["id"]))
            return {
                **result.to_dict(),
                "history": [candle.to_dict() for candle in candles[-120:]],
                "analysis": _build_prediction_analysis(candles, result, horizon),
                "lookback": len(candles),
                "sync": sync_info,
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
        sync_info = {"attempted": False, "updated": False}
        if auto_sync:
            sync_info = _auto_sync_funds(fund_store)
        factors = fund_store.get_latest(symbol, limit=_fund_analysis_history_days())
        if not factors and auto_sync and not sync_info.get("attempted"):
            sync_info = _auto_sync_funds(fund_store, force=True)
            factors = fund_store.get_latest(symbol, limit=_fund_analysis_history_days())
        if not factors:
            if sync_info.get("attempted") and sync_info.get("warning"):
                raise HTTPException(status_code=502, detail=f"资金面同步失败：{sync_info['warning']}")
            raise HTTPException(status_code=404, detail="该股票暂无资金面数据，请等待每日 18:09 同步后重试。")
        return {
            "symbol": symbol,
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


def _build_predictor() -> KronosPredictor:
    return KronosPredictor(
        model_name=os.getenv("KRONOS_MODEL", "NeoQuasar/Kronos-small"),
        tokenizer_name=os.getenv("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base"),
        device=os.getenv("KRONOS_DEVICE", "cpu"),
    )


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
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["*"],
    )


app = create_app()

from __future__ import annotations

import hashlib
import os
from typing import Mapping

import httpx


class PaymentError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class SailaPayClient:
    def __init__(self, base_url: str | None = None, merchant_id: str | None = None, merchant_key: str | None = None):
        self.base_url = (base_url or os.getenv("SAILA_URL") or "https://www.sailapay.com").rstrip("/")
        self.merchant_id = merchant_id or os.getenv("SAILA_ID") or os.getenv("PAYSAILA_PID") or ""
        self.merchant_key = merchant_key or os.getenv("SAILA_KEY") or os.getenv("PAYSAILA_KEY") or ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.merchant_id and self.merchant_key)

    def require_configured(self) -> None:
        if not self.configured:
            raise PaymentError("支付接口尚未配置 SAILA_URL、SAILA_ID、SAILA_KEY。", status_code=503)

    def sign(self, params: Mapping[str, object]) -> str:
        self.require_configured()
        return sign_params(params, self.merchant_key)

    def verify(self, params: Mapping[str, object]) -> bool:
        provided = str(params.get("sign") or "")
        if not provided:
            return False
        expected = self.sign(params)
        return expected == provided.lower()

    def create_payment(
        self,
        *,
        out_trade_no: str,
        name: str,
        money: str,
        notify_url: str,
        return_url: str,
        client_ip: str,
        pay_type: str | None,
        device: str = "pc",
        param: str = "",
    ) -> dict[str, object]:
        self.require_configured()
        payload: dict[str, object] = {
            "pid": self.merchant_id,
            "out_trade_no": out_trade_no,
            "notify_url": notify_url,
            "return_url": return_url,
            "name": name,
            "money": money,
            "clientip": client_ip,
            "device": device,
            "param": param,
            "sign_type": "MD5",
        }
        if pay_type:
            payload["type"] = pay_type
        payload["sign"] = self.sign(payload)

        try:
            response = httpx.post(f"{self.base_url}/mapi.php", data=payload, timeout=20)
            response.raise_for_status()
            result = response.json()
        except httpx.HTTPError as exc:
            raise PaymentError(f"支付接口请求失败：{exc}", status_code=502) from exc
        except ValueError as exc:
            raise PaymentError("支付接口返回内容不是 JSON。", status_code=502) from exc

        if int(result.get("code", 0) or 0) != 1:
            raise PaymentError(str(result.get("msg") or "支付下单失败。"), status_code=502)
        return result


def sign_params(params: Mapping[str, object], key: str) -> str:
    items: list[str] = []
    for name in sorted(params):
        value = params[name]
        if name in {"sign", "sign_type"} or value is None or value == "":
            continue
        items.append(f"{name}={value}")
    raw = "&".join(items) + key
    return hashlib.md5(raw.encode("utf-8")).hexdigest()

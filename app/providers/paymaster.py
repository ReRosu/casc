"""PayMaster API v2: https://paymaster.ru/docs/ru/api"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import PayMasterSettings
from app.domain.declines import PAYMASTER_ERROR_CODES, PAYMASTER_RESULT_CODES, classify
from app.domain.enums import AttemptStatus, DeclineClass, PaymentMethod, ProviderMode, Stage
from app.domain.results import NextAction, ProviderResult
from app.providers.base import (
    HttpAdapter,
    ProviderRequest,
    RefundResult,
    TransportFailure,
    safe_json,
)

_METHODS = {PaymentMethod.BANKCARD: "BankCard", PaymentMethod.SBP: "SBP"}


class PayMasterAdapter(HttpAdapter):
    modes = frozenset({ProviderMode.H2H, ProviderMode.HOSTED})

    def __init__(self, name: str, cfg: PayMasterSettings, *, connect_timeout: float,
                 read_timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(
            name, cfg.base_url,
            {"Authorization": f"Bearer {cfg.token.get_secret_value()}"},
            connect_timeout, read_timeout, transport,
        )
        self._cfg = cfg

    def _base_body(self, req: ProviderRequest) -> dict[str, Any]:
        return {
            "merchantId": self._cfg.merchant_id,
            "testMode": self._cfg.test_mode,
            "invoice": {"description": req.description, "orderNo": req.attempt_id},
            "amount": {"value": str(req.amount), "currency": req.currency},
            "protocol": {"returnUrl": req.return_url, "callbackUrl": req.callback_url},
            "customer": {"email": req.customer_email} if req.customer_email else None,
        }

    async def charge(self, req: ProviderRequest) -> ProviderResult:
        assert req.card is not None, "charge requires card data"
        body = self._base_body(req) | {
            "paymentData": {
                "paymentMethod": "BankCard",
                "card": {"pan": req.card.pan, "expiry": req.card.expiry, "cvc": req.card.cvc},
            }
        }
        try:
            resp = await self._send(Stage.CHARGE, "POST", "/api/v2/payments", json=body,
                                    headers={"Idempotency-Key": req.idempotency_key})
        except TransportFailure as f:
            return f.result
        data = safe_json(resp)
        if resp.is_error:
            return self._map_error(resp.status_code, data)
        return self._map_payment(data, return_url=req.return_url)

    async def create_link(self, req: ProviderRequest) -> ProviderResult:
        body = self._base_body(req)
        # Счёт сам «протухнет», если каскад ушёл дальше, а погасить его мы не смогли.
        body["invoice"]["expires"] = (
            datetime.now(UTC) + timedelta(seconds=req.link_ttl_seconds)
        ).isoformat()
        body["paymentMethod"] = _METHODS[req.method]
        try:
            resp = await self._send(Stage.LINK, "POST", "/api/v2/invoices", json=body,
                                    headers={"Idempotency-Key": req.idempotency_key})
        except TransportFailure as f:
            return f.result
        data = safe_json(resp)
        if resp.is_error:
            return self._map_error(resp.status_code, data)
        return ProviderResult(
            status=AttemptStatus.REQUIRES_ACTION,
            provider_payment_id=str(data["paymentId"]),
            next_action=NextAction(type="redirect", url=data["url"]),
            raw=data,
        )

    async def complete_3ds(self, provider_payment_id: str, params: dict[str, str]) -> ProviderResult:
        body = {k: v for k, v in {
            "PARes": params.get("PaRes") or params.get("PARes"),
            "cres": params.get("cres"),
        }.items() if v}
        try:
            resp = await self._send(Stage.CHARGE, "PUT",
                                    f"/api/v2/payments/{provider_payment_id}/complete", json=body)
        except TransportFailure as f:
            return f.result
        data = safe_json(resp)
        if resp.is_error:
            return self._map_error(resp.status_code, data)
        return self._map_payment(data)

    async def get_status(self, provider_payment_id: str) -> ProviderResult:
        try:
            resp = await self._send(Stage.LINK, "GET", f"/api/v2/payments/{provider_payment_id}")
        except TransportFailure as f:
            return f.result
        data = safe_json(resp)
        if resp.is_error:
            return self._map_error(resp.status_code, data)
        return self._map_payment(data)

    async def find_by_order(self, attempt_id: str, created_after: datetime) -> ProviderResult | None:
        params: dict[str, str] = {
            "merchantId": self._cfg.merchant_id,
            "start": (created_after - timedelta(minutes=1)).isoformat(),
            "end": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        }
        while True:
            resp = await self._send(Stage.LINK, "GET", "/api/v2/payments", params=params)
            resp.raise_for_status()
            data = safe_json(resp)
            for item in data.get("items", []):
                if (item.get("invoice") or {}).get("orderNo") == attempt_id:
                    return self._map_payment(item)
            if not data.get("cursor"):
                return None
            params["cursor"] = data["cursor"]

    async def cancel_link(self, provider_payment_id: str) -> bool:
        try:
            resp = await self._send(Stage.LINK, "PUT",
                                    f"/api/v2/payments/{provider_payment_id}/cancel")
        except TransportFailure:
            return False
        return resp.is_success

    async def refund(self, provider_payment_id: str, amount: Decimal, currency: str,
                     idempotency_key: str) -> RefundResult:
        body = {"paymentId": provider_payment_id,
                "amount": {"value": str(amount), "currency": currency}}
        try:
            resp = await self._send(Stage.LINK, "POST", "/api/v2/refunds", json=body,
                                    headers={"Idempotency-Key": idempotency_key})
        except TransportFailure as f:
            return RefundResult(ok=False, message=f.result.decline_code)
        data = safe_json(resp)
        ok = resp.is_success and data.get("status") in ("Pending", "Success")
        return RefundResult(ok=ok, refund_id=data.get("id"), status=data.get("status"),
                            message=data.get("message"))

    # --- маппинг ---

    def _map_payment(self, data: dict[str, Any], return_url: str | None = None) -> ProviderResult:
        status = data.get("status")
        ppid = str(data["id"]) if data.get("id") is not None else None
        code = data.get("resultCode")
        match status:
            case "Settled" | "Authorized":
                return ProviderResult(AttemptStatus.SUCCEEDED, ppid, raw=data)
            case "Confirmation":
                return ProviderResult(AttemptStatus.REQUIRES_ACTION, ppid,
                                      next_action=self._next_action(data, ppid, return_url),
                                      raw=data)
            case "Rejected" | "Cancelled":
                code = code or status
                return ProviderResult(
                    AttemptStatus.DECLINED, ppid, decline_code=code,
                    decline_class=classify(PAYMASTER_RESULT_CODES, code),
                    message=data.get("message"), raw=data,
                )
            case _:  # Pending и всё незнакомое: исход ещё не известен
                return ProviderResult(AttemptStatus.UNKNOWN, ppid, decline_code="pending",
                                      decline_class=DeclineClass.UNKNOWN, raw=data)

    @staticmethod
    def _next_action(data: dict[str, Any], ppid: str | None,
                     return_url: str | None) -> NextAction | None:
        conf = data.get("confirmation") or {}
        match conf.get("type"):
            case "ThreeDSv1":
                params = {"PaReq": conf.get("PAReq", ""), "MD": ppid or ""}
                if return_url:
                    params["TermUrl"] = return_url
                return NextAction(type="3ds", url=conf["acsUrl"], method="POST", params=params)
            case "ThreeDSv2":
                return NextAction(type="3ds", url=conf["acsUrl"], method="POST",
                                  params={"creq": conf.get("creq", "")})
            case "External":
                return NextAction(type="redirect", url=conf["paymentUrl"])
            case _:
                return None

    @staticmethod
    def _map_error(http_status: int, data: dict[str, Any]) -> ProviderResult:
        code = data.get("code") or ("not_authorized" if http_status in (401, 403) else
                                    f"http_{http_status}")
        cls = classify(PAYMASTER_ERROR_CODES, code)
        if cls is DeclineClass.UNKNOWN:
            return ProviderResult(AttemptStatus.UNKNOWN, decline_code=code,
                                  decline_class=cls, message=data.get("message"), raw=data)
        return ProviderResult.error(code, cls, data.get("message"), raw=data)

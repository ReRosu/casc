"""Pally API v1: https://pally.info/reference/api — только hosted (ссылка на свою страницу)."""

import hashlib
import hmac
import logging
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import PallySettings
from app.domain.declines import PALLY_ERROR_CODES, classify
from app.domain.enums import AttemptStatus, DeclineClass, PaymentMethod, ProviderMode, Stage
from app.domain.results import NextAction, ProviderResult
from app.providers.base import (
    HttpAdapter,
    ProviderRequest,
    RefundResult,
    TransportFailure,
    safe_json,
)

logger = logging.getLogger(__name__)

_METHODS = {PaymentMethod.BANKCARD: "BANK_CARD", PaymentMethod.SBP: "SBP"}


def pally_signature(out_sum: str, inv_id: str, token: str) -> str:
    return hashlib.md5(f"{out_sum}:{inv_id}:{token}".encode()).hexdigest().upper()


class PallyAdapter(HttpAdapter):
    modes = frozenset({ProviderMode.HOSTED})

    def __init__(self, name: str, cfg: PallySettings, *, connect_timeout: float,
                 read_timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(
            name, cfg.base_url,
            {"Authorization": f"Bearer {cfg.token.get_secret_value()}",
             "Accept": "application/json"},
            connect_timeout, read_timeout, transport,
        )
        self._cfg = cfg

    async def charge(self, req: ProviderRequest) -> ProviderResult:
        raise NotImplementedError("Pally не принимает карту напрямую (только hosted)")

    async def complete_3ds(self, provider_payment_id: str, params: dict[str, str]) -> ProviderResult:
        raise NotImplementedError("3DS проходит на странице Pally")

    async def create_link(self, req: ProviderRequest) -> ProviderResult:
        form: dict[str, str] = {
            "amount": str(req.amount),
            "shop_id": self._cfg.shop_id,
            "order_id": req.attempt_id,
            "description": req.description,
            "type": "normal",
            "currency_in": req.currency,
            "payment_method": _METHODS[req.method],
            "success_url": f"{req.return_url}?result=success",
            "fail_url": f"{req.return_url}?result=fail",
            "ttl": str(req.link_ttl_seconds),
            # Один отказ на странице — ссылка мертва, повторных попыток по ней не будет.
            "deactivate_on_fail": "1",
        }
        if req.customer_email:
            form["payer_data[email]"] = req.customer_email
        try:
            resp = await self._send(Stage.LINK, "POST", "/api/v1/bill/create", data=form)
        except TransportFailure as f:
            return f.result
        data = safe_json(resp)
        if resp.is_error or str(data.get("success")).lower() != "true":
            return self._map_error(resp.status_code, data)
        return ProviderResult(
            status=AttemptStatus.REQUIRES_ACTION,
            provider_payment_id=data["bill_id"],
            next_action=NextAction(type="redirect", url=data["link_page_url"]),
            raw=data,
        )

    async def get_status(self, provider_payment_id: str) -> ProviderResult:
        try:
            resp = await self._send(Stage.LINK, "GET", "/api/v1/bill/status",
                                    params={"id": provider_payment_id})
        except TransportFailure as f:
            return f.result
        data = safe_json(resp)
        if resp.is_error:
            return self._map_error(resp.status_code, data)
        return self._map_bill(data)

    async def find_by_order(self, attempt_id: str, created_after: datetime) -> ProviderResult | None:
        resp = await self._send(Stage.LINK, "GET", "/api/v1/bill/search",
                                params={"order_id": attempt_id, "shop_id": self._cfg.shop_id})
        resp.raise_for_status()
        bills = safe_json(resp).get("data") or []
        return self._map_bill(bills[0]) if bills else None

    async def cancel_link(self, provider_payment_id: str) -> bool:
        try:
            resp = await self._send(Stage.LINK, "POST", "/api/v1/bill/toggle_activity",
                                    data={"id": provider_payment_id, "active": "0"})
        except TransportFailure:
            return False
        return resp.is_success

    async def refund(self, provider_payment_id: str, amount: Decimal, currency: str,
                     idempotency_key: str) -> RefundResult:
        # provider_payment_id у нас — bill_id; для возврата нужен id платежа внутри счёта.
        try:
            resp = await self._send(Stage.LINK, "GET", "/api/v1/bill/payments",
                                    params={"id": provider_payment_id})
            payments = safe_json(resp).get("data") or []
            paid = next((p for p in payments if p.get("status") in ("SUCCESS", "OVERPAID")), None)
            if paid is None:
                return RefundResult(ok=False, message="no successful payment in bill")
            resp = await self._send(Stage.LINK, "POST", "/api/v1/refund/full/create",
                                    data={"payment_id": paid["id"], "order_id": idempotency_key})
        except TransportFailure as f:
            return RefundResult(ok=False, message=f.result.decline_code)
        data = safe_json(resp)
        ok = resp.is_success and data.get("status") in ("NEW", "PROCESS", "SUCCESS")
        return RefundResult(ok=ok, refund_id=data.get("id"), status=data.get("status"))

    # --- postback ---

    def verify_postback(self, form: Mapping[str, str]) -> bool:
        expected = pally_signature(form.get("OutSum", ""), form.get("InvId", ""),
                                   self._cfg.token.get_secret_value())
        received = form.get("SignatureValue", "").upper().encode()
        return hmac.compare_digest(expected.encode(), received)

    @staticmethod
    def postback_to_result(form: Mapping[str, str]) -> ProviderResult:
        raw = dict(form)
        raw.pop("SignatureValue", None)
        return PallyAdapter._map_status(form.get("Status"), form.get("TrsId"),
                                        form.get("ErrorCode"), form.get("ErrorMessage"), raw)

    # --- маппинг ---

    @staticmethod
    def _map_bill(data: dict[str, Any]) -> ProviderResult:
        return PallyAdapter._map_status(data.get("status"), data.get("id"), None, None, data)

    @staticmethod
    def _map_status(status: str | None, bill_id: str | None, error_code: str | None,
                    error_message: str | None, raw: dict[str, Any]) -> ProviderResult:
        match status:
            case "SUCCESS" | "OVERPAID":
                return ProviderResult(AttemptStatus.SUCCEEDED, bill_id, raw=raw)
            case "FAIL":
                return ProviderResult(AttemptStatus.DECLINED, bill_id,
                                      decline_code=error_code or "FAIL",
                                      decline_class=DeclineClass.SOFT,
                                      message=error_message, raw=raw)
            case "UNDERPAID":
                # Деньги частично пришли: платёж не засчитываем, нужен ручной разбор/возврат.
                logger.warning("pally bill %s UNDERPAID — needs manual review", bill_id)
                return ProviderResult(AttemptStatus.DECLINED, bill_id, decline_code="underpaid",
                                      decline_class=DeclineClass.HARD, raw=raw)
            case _:  # NEW / PROCESS — клиент ещё на странице оплаты
                return ProviderResult(AttemptStatus.REQUIRES_ACTION, bill_id, raw=raw)

    @staticmethod
    def _map_error(http_status: int, data: dict[str, Any]) -> ProviderResult:
        if http_status == 401:
            code = "Unauthenticated"
        elif http_status == 422:
            code = "validation_error"
        else:
            code = data.get("error") or data.get("message") or f"http_{http_status}"
        return ProviderResult.error(code, classify(PALLY_ERROR_CODES, code),
                                    data.get("message"), raw=data)

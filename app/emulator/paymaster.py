"""Эмулятор PayMaster API v2 (подмножество, нужное каскаду). Один экземпляр = один эквайер."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.emulator.common import Outage, page, send_callback
from app.emulator.config import get_emulator_settings

# Тестовые карты: PAN → поведение по экземплярам ("*" — по умолчанию). Без записи — успех.
TEST_CARDS: dict[str, dict[str, str]] = {
    "4111111111111111": {"*": "success"},
    # Мягкий отказ на шлюзе A, успех на B — классический каскад.
    "4000000000000002": {"paymaster_a": "decline:TransactionDeclined", "*": "success"},
    # Мягкий отказ на обоих шлюзах — уходим на hosted-страницу Pally.
    "4000000000000341": {"*": "decline:TransactionDeclined"},
    # Жёсткие отказы — каскад останавливается на первой попытке.
    "4000000000009995": {"*": "decline:InsufficientFunds"},
    "4000000000000127": {"*": "decline:RejectedByFraud"},
    # 3DS: успех / мягкий отказ после 3DS (→ hosted fallback).
    "4000000000003220": {"*": "3ds"},
    "4000000000003238": {"paymaster_a": "3ds_then_decline:TransactionDeclined", "*": "success"},
    # Технические сбои шлюза A.
    "4000000000000119": {"paymaster_a": "http500", "*": "success"},
    "4000000000000259": {"paymaster_a": "timeout_not_created", "*": "success"},
    "4000000000000267": {"paymaster_a": "timeout_charged", "*": "success"},
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def create_paymaster_app(name: str, token: str) -> FastAPI:
    app = FastAPI(title=f"PayMaster emulator ({name})")
    prefix = name.replace("_", "-")
    payments: dict[str, dict[str, Any]] = {}
    idempotency: dict[str, dict[str, Any]] = {}
    callbacks: dict[str, str] = {}
    scenarios: dict[str, str] = {}
    outage = Outage()
    settings = get_emulator_settings()

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if "/api/" in request.url.path:  # у mount-приложения путь включает префикс
            if outage.active:
                return _err(503, "service_unavailable", "Service temporarily unavailable")
            if request.headers.get("Authorization") != f"Bearer {token}":
                return _err(401, "not_authorized", "Unauthenticated")
        return await call_next(request)

    def _new_payment(body: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
        pid = str(len(payments) + 10000)
        payment = {
            "id": pid, "created": _now(), "testMode": body.get("testMode", False),
            "status": status, "merchantId": body.get("merchantId"),
            "invoice": body.get("invoice", {}), "amount": body.get("amount", {}),
            "paymentData": {"paymentMethod": "BankCard"},
        } | extra
        payments[pid] = payment
        if cb := (body.get("protocol") or {}).get("callbackUrl"):
            callbacks[pid] = cb
        return payment

    def _notify(payment: dict[str, Any]) -> None:
        if cb := callbacks.get(payment["id"]):
            send_callback(cb, json={k: v for k, v in payment.items() if k != "confirmation"})

    @app.post("/api/v2/payments")
    async def create_payment(request: Request) -> JSONResponse:
        body = await request.json()
        key = request.headers.get("Idempotency-Key")
        if key and key in idempotency:  # повтор с тем же ключом — тот же результат
            return JSONResponse(idempotency[key])
        pan = ((body.get("paymentData") or {}).get("card") or {}).get("pan", "")
        rules = TEST_CARDS.get(pan, {})
        behavior = rules.get(name) or rules.get("*") or "success"
        title = f"{pan[:6]}******{pan[-4:]}"

        if behavior == "http500":
            return _err(500, "internal_error", "Internal server error")
        if behavior == "timeout_not_created":
            await asyncio.sleep(settings.timeout_sleep)
            return _err(500, "internal_error", "request dropped")
        if behavior == "timeout_charged":
            payment = _new_payment(body, "Settled")
            payment["paymentData"]["paymentInstrumentTitle"] = title
            await asyncio.sleep(settings.timeout_sleep)
            return JSONResponse(payment)

        if behavior == "success":
            payment = _new_payment(body, "Settled", resultCode="Success")
        elif behavior.startswith("decline:"):
            code = behavior.split(":", 1)[1]
            payment = _new_payment(body, "Rejected", resultCode=code, message=code)
        else:  # 3ds / 3ds_then_decline
            payment = _new_payment(body, "Confirmation")
            payment["confirmation"] = {
                "type": "ThreeDSv1",
                "acsUrl": f"{settings.public_url}/{prefix}/acs/{payment['id']}",
                "PAReq": f"pareq-{payment['id']}",
            }
            scenarios[payment["id"]] = behavior
        payment["paymentData"]["paymentInstrumentTitle"] = title
        if key:
            idempotency[key] = payment
        if payment["status"] != "Confirmation":
            _notify(payment)
        return JSONResponse(payment)

    @app.put("/api/v2/payments/{pid}/complete")
    async def complete(pid: str, request: Request) -> JSONResponse:
        payment = payments.get(pid)
        if payment is None:
            return _err(404, "not_found", "Payment not found")
        if payment["status"] != "Confirmation":
            return JSONResponse(payment)
        body = await request.json()
        payment.pop("confirmation", None)
        behavior = scenarios.get(pid, "3ds")
        if body.get("PARes") != "ok":
            payment.update(status="Rejected", resultCode="ThreeDSecureFailed")
        elif behavior.startswith("3ds_then_decline:"):
            code = behavior.split(":", 1)[1]
            payment.update(status="Rejected", resultCode=code)
        else:
            payment.update(status="Settled", resultCode="Success", completed=_now())
        _notify(payment)
        return JSONResponse(payment)

    @app.get("/api/v2/payments/{pid}")
    async def get_payment(pid: str) -> JSONResponse:
        payment = payments.get(pid)
        return JSONResponse(payment) if payment else _err(404, "not_found", "Payment not found")

    @app.get("/api/v2/payments")
    async def list_payments(merchantId: str) -> JSONResponse:  # noqa: N803
        items = [p for p in payments.values() if p.get("merchantId") == merchantId]
        return JSONResponse({"items": items})

    @app.put("/api/v2/payments/{pid}/cancel")
    async def cancel(pid: str) -> JSONResponse:
        payment = payments.get(pid)
        if payment is None:
            return _err(404, "not_found", "Payment not found")
        if payment["status"] in ("Settled", "Authorized"):
            return _err(400, "invalid_operation", "Payment already completed")
        payment["status"] = "Cancelled"
        return JSONResponse({})

    @app.post("/api/v2/invoices")
    async def create_invoice(request: Request) -> JSONResponse:
        body = await request.json()
        key = request.headers.get("Idempotency-Key")
        if key and key in idempotency:
            return JSONResponse(idempotency[key])
        payment = _new_payment(body, "Pending")
        payment["returnUrl"] = (body.get("protocol") or {}).get("returnUrl")
        resp = {"paymentId": payment["id"],
                "url": f"{settings.public_url}/{prefix}/cpay/{payment['id']}"}
        if key:
            idempotency[key] = resp
        return JSONResponse(resp)

    @app.post("/api/v2/refunds")
    async def refund(request: Request) -> JSONResponse:
        body = await request.json()
        payment = payments.get(str(body.get("paymentId")))
        if payment is None or payment["status"] != "Settled":
            return _err(400, "invalid_operation", "Payment cannot be refunded")
        payment["refunded"] = True
        return JSONResponse({"id": str(uuid.uuid4().int)[:6], "created": _now(),
                             "status": "Success", "paymentId": payment["id"],
                             "amount": body.get("amount")})

    # ---- страницы для браузера клиента ----

    @app.post("/acs/{pid}", response_class=HTMLResponse)
    async def acs_page(pid: str, TermUrl: str = Form(""), MD: str = Form("")) -> str:  # noqa: N803
        form = (f'<form method="post" action="{TermUrl}"><input type="hidden" name="MD" '
                f'value="{MD}"><button name="PaRes" value="ok">Подтвердить</button>'
                f'<button name="PaRes" value="fail">Отклонить</button></form>')
        return page(f"3-D Secure ({name}) — платёж {pid}", form)

    @app.get("/cpay/{pid}", response_class=HTMLResponse)
    async def hosted_page(pid: str) -> str:
        payment = payments.get(pid)
        if payment is None or payment["status"] != "Pending":
            return page("Счёт недоступен", "<p>Счёт оплачен, отменён или не существует.</p>")
        amount = payment["amount"]
        form = (f'<p>{amount.get("value")} {amount.get("currency")}</p>'
                f'<form method="post" action="/{prefix}/cpay/{pid}/pay">'
                '<button name="result" value="success">Оплатить</button>'
                '<button name="result" value="fail">Отказ</button></form>')
        return page(f"PayMaster ({name})", form)

    @app.post("/cpay/{pid}/pay", response_model=None)
    async def hosted_pay(pid: str, result: str = Form(...)) -> RedirectResponse | JSONResponse:
        payment = payments.get(pid)
        if payment is None or payment["status"] != "Pending":
            return _err(410, "invalid_operation", "Invoice is not payable")
        if result == "success":
            payment.update(status="Settled", resultCode="Success", completed=_now())
        else:
            payment.update(status="Rejected", resultCode="TransactionDeclined")
        _notify(payment)
        return RedirectResponse(payment.get("returnUrl") or "/", status_code=303)

    # ---- управление сценариями ----

    @app.post("/_admin/outage")
    async def set_outage(seconds: float = 60) -> dict[str, Any]:
        outage.set(seconds) if seconds > 0 else outage.clear()
        return {"provider": name, "outage_seconds": seconds}

    return app

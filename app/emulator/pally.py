"""Эмулятор Pally API v1 (подмножество). Сценарии ошибок выбираются копейками суммы."""

import asyncio
import random
import string
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.emulator.common import Outage, page, send_callback
from app.emulator.config import get_emulator_settings
from app.providers.pally import pally_signature

# Копейки суммы → сценарий ответа на bill/create.
AMOUNT_SCENARIOS: dict[str, str] = {
    "01": "rate_not_found",  # направление недоступно → SOFT
    "02": "timeout",  # счёт создан, но ответ не дошёл → UNKNOWN, осиротевший счёт
    "03": "unauthenticated",  # 401 → CONFIG
    "04": "invalid_amount",  # 403 invalid_amount → HARD
    "05": "http500",  # 500 → SOFT
    "06": "transaction_rejected",  # 400 → SOFT
}


def _id() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=10))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _fail(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"success": False, "error": error, "message": message},
                        status_code=status)


def create_pally_app(token: str) -> FastAPI:
    app = FastAPI(title="Pally emulator")
    bills: dict[str, dict[str, Any]] = {}
    bill_payments: dict[str, dict[str, Any]] = {}
    outage = Outage()
    settings = get_emulator_settings()

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if "/api/" in request.url.path:  # у mount-приложения путь включает префикс
            if outage.active:
                return _fail(503, "api:error.general_error", "Service unavailable")
            if request.headers.get("Authorization") != f"Bearer {token}":
                return JSONResponse({"message": "Unauthenticated."}, status_code=401)
        return await call_next(request)

    def _public_bill(b: dict[str, Any]) -> dict[str, Any]:
        return {k: b[k] for k in ("id", "order_id", "active", "status", "amount", "type",
                                  "created_at", "currency_in", "ttl")}

    @app.post("/api/v1/bill/create")
    async def create_bill(
        amount: Decimal = Form(...), shop_id: str = Form(...), order_id: str = Form(""),
        currency_in: str = Form("RUB"), ttl: int = Form(600), success_url: str = Form(""),
        fail_url: str = Form(""), deactivate_on_fail: int = Form(0),
    ) -> JSONResponse:
        scenario = AMOUNT_SCENARIOS.get(f"{amount:.2f}"[-2:])
        match scenario:
            case "rate_not_found":
                return _fail(403, "api:error.rate-not-found", "Направление недоступно")
            case "unauthenticated":
                return JSONResponse({"message": "Unauthenticated."}, status_code=401)
            case "invalid_amount":
                return _fail(403, "api:error.invalid_amount", "Неверная сумма")
            case "http500":
                return _fail(500, "api:error.general_error", "Внутренняя ошибка")
            case "transaction_rejected":
                return _fail(400, "api:error.transaction_rejected", "Транзакция отклонена")
        bill_id = _id()
        bills[bill_id] = {
            "id": bill_id, "order_id": order_id, "active": True, "status": "NEW",
            "amount": float(amount), "type": "NORMAL", "created_at": _now(),
            "currency_in": currency_in, "ttl": ttl, "shop_id": shop_id,
            "success_url": success_url, "fail_url": fail_url,
            "deactivate_on_fail": bool(deactivate_on_fail),
        }
        if scenario == "timeout":
            await asyncio.sleep(settings.timeout_sleep)
        return JSONResponse({
            "success": "true",
            "link_url": f"{settings.public_url}/pally/link/{bill_id}",
            "link_page_url": f"{settings.public_url}/pally/transfer/{bill_id}",
            "bill_id": bill_id,
        })

    @app.get("/api/v1/bill/status")
    async def bill_status(id: str) -> JSONResponse:  # noqa: A002
        bill = bills.get(id)
        if bill is None:
            return _fail(404, "api:error.not_found", "Счёт не найден")
        return JSONResponse(_public_bill(bill) | {"success": True})

    @app.get("/api/v1/bill/search")
    async def bill_search(order_id: str | None = None, shop_id: str | None = None) -> JSONResponse:
        data = [_public_bill(b) for b in bills.values()
                if (order_id is None or b["order_id"] == order_id)
                and (shop_id is None or b["shop_id"] == shop_id)]
        return JSONResponse({"success": True, "data": data, "links": {}, "meta": {}})

    @app.post("/api/v1/bill/toggle_activity")
    async def toggle(id: str = Form(...), active: int = Form(...)) -> JSONResponse:  # noqa: A002
        bill = bills.get(id)
        if bill is None:
            return _fail(404, "api:error.not_found", "Счёт не найден")
        bill["active"] = bool(active)
        return JSONResponse(_public_bill(bill) | {"success": True})

    @app.get("/api/v1/bill/payments")
    async def payments_of_bill(id: str) -> JSONResponse:  # noqa: A002
        data = [p for p in bill_payments.values() if p["bill_id"] == id]
        return JSONResponse({"success": True, "data": data})

    @app.post("/api/v1/refund/full/create")
    async def refund(payment_id: str = Form(...)) -> JSONResponse:
        payment = bill_payments.get(payment_id)
        if payment is None or payment["status"] != "SUCCESS":
            return _fail(400, "api:error.transaction_rejected", "Возврат невозможен")
        payment["refunded_amount"] = payment["amount"]
        return JSONResponse({"id": _id(), "status": "NEW", "amount": payment["amount"],
                             "currency": payment["currency_in"], "entity_type": "payment",
                             "entity_id": payment_id, "created_at": _now(), "success": True})

    # ---- страница оплаты ----

    @app.get("/transfer/{bill_id}", response_class=HTMLResponse)
    async def transfer_page(bill_id: str) -> str:
        bill = bills.get(bill_id)
        if bill is None or not bill["active"] or bill["status"] != "NEW":
            return page("Ссылка недействительна", "<p>Счёт оплачен или деактивирован.</p>")
        form = (f'<p>{bill["amount"]} {bill["currency_in"]}</p>'
                f'<form method="post" action="/pally/transfer/{bill_id}/pay">'
                '<button name="result" value="success">Оплатить</button>'
                '<button name="result" value="fail">Отказ</button></form>')
        return page("Pally", form)

    @app.post("/transfer/{bill_id}/pay", response_model=None)
    async def pay(bill_id: str, result: str = Form(...)) -> RedirectResponse | JSONResponse:
        bill = bills.get(bill_id)
        if bill is None or not bill["active"] or bill["status"] != "NEW":
            return _fail(410, "api:error.bill_inactive", "Счёт неактивен")
        success = result == "success"
        bill["status"] = "SUCCESS" if success else "FAIL"
        if not success and bill["deactivate_on_fail"]:
            bill["active"] = False
        payment_id = _id()
        bill_payments[payment_id] = {
            "id": payment_id, "bill_id": bill_id, "status": bill["status"],
            "amount": bill["amount"], "currency_in": bill["currency_in"],
            "created_at": _now(),
        }
        out_sum = f"{bill['amount']:.2f}"
        postback = {
            "InvId": bill["order_id"], "OutSum": out_sum, "Commission": "0.00",
            "TrsId": bill_id, "Status": bill["status"], "CurrencyIn": bill["currency_in"],
            "AccountType": "BANK_CARD", "AccountNumber": "220220******7046",
            "SignatureValue": pally_signature(out_sum, bill["order_id"], token),
        }
        if not success:
            postback |= {"ErrorCode": "666", "ErrorMessage": "Отказ эмитента"}
        send_callback(settings.pally_result_url, data=postback)
        return RedirectResponse(bill["success_url" if success else "fail_url"] or "/",
                                status_code=303)

    @app.post("/_admin/outage")
    async def set_outage(seconds: float = 60) -> dict[str, Any]:
        outage.set(seconds) if seconds > 0 else outage.clear()
        return {"provider": "pally", "outage_seconds": seconds}

    return app

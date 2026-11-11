"""Возврат браузера клиента: с 3DS (ACS шлёт POST с PaRes) и с hosted-страницы провайдера."""

import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.api.schemas import PaymentOut
from app.cascade.engine import CascadeEngine, NotFound
from app.cascade.factory import get_cascade
from app.db.models import Payment

router = APIRouter(prefix="/v1/return", tags=["return"])

Engine = Annotated[CascadeEngine, Depends(get_cascade)]


@router.post("/{attempt_id}")
async def return_from_3ds(attempt_id: uuid.UUID, request: Request, engine: Engine) -> Response:
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    try:
        payment = await engine.complete_3ds(attempt_id, params)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attempt not found") from exc
    return _respond(payment)


@router.get("/{attempt_id}")
async def return_from_hosted(attempt_id: uuid.UUID, engine: Engine) -> Response:
    # Параметр ?result=success из URL не используем: браузеру не верим, спрашиваем провайдера.
    try:
        payment = await engine.sync_attempt(attempt_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attempt not found") from exc
    return _respond(payment)


def _respond(payment: Payment) -> Response:
    out = PaymentOut.from_model(payment)
    # Каскад ушёл на hosted-провайдера (напр. мягкий отказ после 3DS) — сразу ведём туда.
    if out.next_action and out.next_action.get("type") == "redirect":
        return RedirectResponse(out.next_action["url"], status.HTTP_303_SEE_OTHER)
    if payment.return_url:
        sep = "&" if "?" in payment.return_url else "?"
        query = urlencode({"payment_id": str(payment.id), "status": payment.status.value})
        return RedirectResponse(f"{payment.return_url}{sep}{query}", status.HTTP_303_SEE_OTHER)
    return JSONResponse(out.model_dump(mode="json"))

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from app.api.schemas import PaymentCreate, PaymentOut
from app.cascade.engine import (
    CardVelocityExceeded,
    CascadeEngine,
    CreatePayment,
    IdempotencyConflict,
    InvalidState,
    NotFound,
)
from app.cascade.factory import get_cascade

router = APIRouter(prefix="/v1/payments", tags=["payments"])

Engine = Annotated[CascadeEngine, Depends(get_cascade)]


@router.post("", response_model=PaymentOut, status_code=status.HTTP_201_CREATED,
             responses={200: {"description": "Повтор запроса с тем же Idempotency-Key"},
                        409: {"description": "Idempotency-Key уже использован с другим телом"}})
async def create_payment(
    body: PaymentCreate,
    engine: Engine,
    response: Response,
    idempotency_key: Annotated[str, Header(min_length=8, max_length=128)],
) -> PaymentOut:
    cmd = CreatePayment(
        idempotency_key=idempotency_key, flow=body.flow, order_id=body.order_id,
        amount=body.amount, currency=body.currency, description=body.description,
        method=body.method, customer_email=body.customer_email,
        return_url=str(body.return_url) if body.return_url else None,
        card=body.card.to_domain() if body.card else None,
    )
    try:
        payment, created = await engine.create_payment(cmd)
    except IdempotencyConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Idempotency-Key reused with a different request") from exc
    except CardVelocityExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return PaymentOut.from_model(payment)


@router.get("/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: uuid.UUID, engine: Engine) -> PaymentOut:
    try:
        return PaymentOut.from_model(await engine.get_payment(payment_id))
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payment not found") from exc


@router.post("/{payment_id}/refund", response_model=PaymentOut)
async def refund_payment(payment_id: uuid.UUID, engine: Engine) -> PaymentOut:
    try:
        return PaymentOut.from_model(await engine.refund_payment(payment_id))
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payment not found") from exc
    except InvalidState as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

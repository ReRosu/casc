import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator

from app.db.models import Payment, PaymentAttempt
from app.domain.enums import (
    AttemptStatus,
    DeclineClass,
    Flow,
    PaymentMethod,
    PaymentStatus,
    ProviderMode,
    Stage,
)
from app.domain.results import CardData


class CardIn(BaseModel):
    pan: Annotated[SecretStr, Field(min_length=13, max_length=19)]
    expiry: Annotated[str, Field(pattern=r"^(0[1-9]|1[0-2])/\d{2}$", examples=["12/29"])]
    cvc: Annotated[SecretStr, Field(min_length=3, max_length=4)]

    @model_validator(mode="after")
    def _digits(self) -> Self:
        if not self.pan.get_secret_value().isdigit() or not self.cvc.get_secret_value().isdigit():
            raise ValueError("pan and cvc must be digits")
        return self

    def to_domain(self) -> CardData:
        return CardData(self.pan.get_secret_value(), self.expiry, self.cvc.get_secret_value())


class PaymentCreate(BaseModel):
    flow: Flow
    order_id: Annotated[str, Field(min_length=1, max_length=64)]
    amount: Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=2)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "RUB"
    description: Annotated[str, Field(min_length=1, max_length=255)]
    method: PaymentMethod = PaymentMethod.BANKCARD
    customer_email: str | None = None
    return_url: HttpUrl | None = None
    card: CardIn | None = None

    @model_validator(mode="after")
    def _card_matches_flow(self) -> Self:
        if self.flow is Flow.CARD and self.card is None:
            raise ValueError("card is required for flow=card")
        if self.flow is Flow.LINK and self.card is not None:
            raise ValueError("card must not be sent for flow=link")
        if self.flow is Flow.CARD and self.method is not PaymentMethod.BANKCARD:
            raise ValueError("flow=card supports only method=bankcard")
        return self


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    provider: str
    mode: ProviderMode
    stage: Stage
    status: AttemptStatus
    provider_payment_id: str | None
    decline_code: str | None
    decline_class: DeclineClass | None
    message: str | None
    is_duplicate: bool
    latency_ms: int | None
    created_at: datetime


class PaymentOut(BaseModel):
    id: uuid.UUID
    flow: Flow
    order_id: str
    amount: Decimal
    currency: str
    method: PaymentMethod
    status: PaymentStatus
    failure_code: str | None
    next_action: dict[str, Any] | None
    succeeded_attempt_id: uuid.UUID | None
    route: list[dict[str, Any]]
    attempts: list[AttemptOut]
    created_at: datetime

    @classmethod
    def from_model(cls, p: Payment) -> PaymentOut:
        return cls(
            id=p.id, flow=p.flow, order_id=p.merchant_order_id, amount=p.amount,
            currency=p.currency, method=p.method, status=p.status, failure_code=p.failure_code,
            next_action=_next_action(p), succeeded_attempt_id=p.succeeded_attempt_id,
            route=p.route_snapshot, attempts=[AttemptOut.model_validate(a) for a in p.attempts],
            created_at=p.created_at,
        )


def _next_action(p: Payment) -> dict[str, Any] | None:
    if p.status is not PaymentStatus.REQUIRES_ACTION or not p.attempts:
        return None
    last: PaymentAttempt = p.attempts[-1]
    return last.next_action if last.status is AttemptStatus.REQUIRES_ACTION else None


class ProviderOut(BaseModel):
    provider: str
    flow: Flow
    mode: ProviderMode
    priority: int
    enabled: bool
    fee_percent: Decimal
    circuit_open: bool

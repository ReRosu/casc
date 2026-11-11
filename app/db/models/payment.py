from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, TimestampMixin, UUIDPkMixin, str_enum
from app.domain.enums import Flow, PaymentMethod, PaymentStatus

if TYPE_CHECKING:
    from app.db.models.attempt import PaymentAttempt


class Payment(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "payments"

    flow: Mapped[Flow] = mapped_column(str_enum(Flow))
    merchant_order_id: Mapped[str] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str] = mapped_column(String(255))
    method: Mapped[PaymentMethod] = mapped_column(str_enum(PaymentMethod))
    customer_email: Mapped[str | None] = mapped_column(String(255))
    return_url: Mapped[str | None] = mapped_column(String(1024))

    status: Mapped[PaymentStatus] = mapped_column(
        str_enum(PaymentStatus), default=PaymentStatus.CREATED, index=True
    )
    failure_code: Mapped[str | None] = mapped_column(String(64))

    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))

    # Маршрут фиксируется при создании: правила роутинга могут поменяться, а платёж — нет.
    route_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Единственный источник правды «через кого прошли деньги». UNIQUE + один столбец =
    # физически невозможно иметь две «успешные» попытки у платежа.
    succeeded_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_attempts.id", use_alter=True), unique=True
    )

    attempts: Mapped[list[PaymentAttempt]] = relationship(
        back_populates="payment",
        order_by="PaymentAttempt.seq",
        foreign_keys="PaymentAttempt.payment_id",
        lazy="selectin",
    )

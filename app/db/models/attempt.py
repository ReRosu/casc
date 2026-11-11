from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base, TimestampMixin, UUIDPkMixin, str_enum
from app.domain.enums import AttemptStatus, DeclineClass, ProviderMode, Stage

if TYPE_CHECKING:
    from app.db.models.payment import Payment


class PaymentAttempt(UUIDPkMixin, TimestampMixin, Base):
    """Одна попытка провести платёж через одного провайдера.

    id попытки уходит провайдеру как order_id / orderNo — по нему находим попытку
    в вебхуках и при выяснении статуса после таймаута.
    """

    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint("payment_id", "seq"),
        Index("ix_payment_attempts_provider_ppid", "provider", "provider_payment_id"),
        Index("ix_payment_attempts_status", "status"),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32))
    mode: Mapped[ProviderMode] = mapped_column(str_enum(ProviderMode))
    stage: Mapped[Stage] = mapped_column(str_enum(Stage))

    status: Mapped[AttemptStatus] = mapped_column(
        str_enum(AttemptStatus), default=AttemptStatus.PENDING
    )
    provider_payment_id: Mapped[str | None] = mapped_column(String(128))
    decline_code: Mapped[str | None] = mapped_column(String(64))
    decline_class: Mapped[DeclineClass | None] = mapped_column(str_enum(DeclineClass))
    message: Mapped[str | None] = mapped_column(String(512))
    next_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Деньги прошли, но платёж уже оплачен через другую попытку → автоматический возврат.
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Ответ провайдера без PAN/CVC — для разбора инцидентов.
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    payment: Mapped[Payment] = relationship(
        back_populates="attempts", foreign_keys=[payment_id]
    )

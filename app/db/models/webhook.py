from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, UUIDPkMixin


class WebhookEvent(UUIDPkMixin, Base):
    """Inbox входящих уведомлений: сохраняем, отвечаем 200, обрабатываем в воркере.

    UNIQUE(provider, dedup_key) — повторная доставка того же события не обрабатывается дважды.
    """

    __tablename__ = "webhook_inbox"
    __table_args__ = (UniqueConstraint("provider", "dedup_key"),)

    provider: Mapped[str] = mapped_column(String(32))
    dedup_key: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signature_ok: Mapped[bool | None] = mapped_column(Boolean)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    process_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

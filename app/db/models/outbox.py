import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, UUIDPkMixin


class OutboxTask(UUIDPkMixin, Base):
    """Фоновая задача, записанная в той же транзакции, что и изменение состояния попытки.

    Без этого между commit и постановкой в очередь есть окно: упал процесс или Redis —
    задача (гашение ссылки, возврат дубля, выяснение UNKNOWN) потеряна. С outbox она
    остаётся в БД, пока не будет отправлена; reconcile досылает неотправленные.
    """

    __tablename__ = "outbox"

    kind: Mapped[str] = mapped_column(String(32))
    attempt_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

"""Чистка: обработанные вебхуки и отправленные задачи outbox не должны копиться вечно.
Платежи и попытки не удаляются — это финансовая история."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import OutboxTask, WebhookEvent


async def purge_old_data(sm: async_sessionmaker[AsyncSession], settings: Settings,
                         *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    async with sm() as s, s.begin():
        webhooks = await s.execute(delete(WebhookEvent).where(
            WebhookEvent.processed_at.is_not(None),
            WebhookEvent.processed_at < now - timedelta(days=settings.webhook_retention_days)))
        outbox = await s.execute(delete(OutboxTask).where(
            OutboxTask.sent_at.is_not(None),
            OutboxTask.sent_at < now - timedelta(days=settings.outbox_retention_days)))
    return {"webhook_inbox": webhooks.rowcount, "outbox": outbox.rowcount}  # type: ignore[attr-defined]

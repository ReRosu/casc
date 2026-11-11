"""Inbox вебхуков: сохранить → ответить 200 → обработать в воркере.

Любой вебхук для нас — сигнал «сходи и проверь статус через API провайдера»:
у PayMaster подписи нет вовсе (её роль играет HMAC в callback URL), у Pally подпись
не покрывает поле Status.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cascade.engine import CascadeEngine, NotFound
from app.db.models import WebhookEvent

logger = logging.getLogger(__name__)


async def store_event(sm: async_sessionmaker[AsyncSession], provider: str, dedup_key: str,
                      payload: dict[str, Any], signature_ok: bool | None) -> uuid.UUID | None:
    """Сохранить событие. None — дубль (провайдер прислал повторно), обрабатывать не надо."""
    async with sm() as s, s.begin():
        stmt = (
            insert(WebhookEvent)
            .values(id=uuid.uuid7(), provider=provider, dedup_key=dedup_key, payload=payload,
                    signature_ok=signature_ok, process_attempts=0)
            .on_conflict_do_nothing(index_elements=["provider", "dedup_key"])
            .returning(WebhookEvent.id)
        )
        return await s.scalar(stmt)


async def process_event(sm: async_sessionmaker[AsyncSession], engine: CascadeEngine,
                        event_id: uuid.UUID) -> None:
    async with sm() as s:
        event = await s.get(WebhookEvent, event_id)
    if event is None or event.processed_at is not None:
        return
    error: str | None = None
    try:
        await _handle(engine, event)
    except Exception as exc:
        logger.exception("webhook %s processing failed", event_id)
        error = repr(exc)[:1000]
    async with sm() as s, s.begin():
        ev = await s.get(WebhookEvent, event_id, with_for_update=True)
        assert ev is not None
        ev.process_attempts += 1
        ev.last_error = error
        if error is None:
            ev.processed_at = datetime.now(UTC)
    if error is not None:
        raise RuntimeError(error)


async def _handle(engine: CascadeEngine, event: WebhookEvent) -> None:
    payload = event.payload
    raw_id = payload.get("InvId") if event.provider == "pally" else payload.get("_attempt_id")
    attempt_id = _parse_uuid(raw_id)
    if attempt_id is None:
        logger.warning("%s webhook for unknown attempt %r — ignored", event.provider, raw_id)
        return
    try:
        await engine.get_attempt(attempt_id)
    except NotFound:
        logger.warning("%s webhook for unknown attempt %s — ignored", event.provider, attempt_id)
        return
    # Содержимое вебхука — только повод. Факт берём у провайдера; если он недоступен —
    # исключение, задача повторится, а reconcile подберёт необработанное событие.
    await engine.sync_attempt(attempt_id, strict=True)


def _parse_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None

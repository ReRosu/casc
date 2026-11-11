import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from taskiq import AsyncTaskiqDecoratedTask

from app.cascade.factory import get_cascade
from app.cascade.inbox import process_event
from app.cascade.retention import purge_old_data
from app.core.config import get_settings
from app.core.redis import get_redis
from app.db.db import get_sessionmaker
from app.db.models import Payment, PaymentAttempt, WebhookEvent
from app.domain.enums import AttemptStatus, PaymentStatus, ProviderMode, Stage
from app.worker.broker import broker

logger = logging.getLogger(__name__)

# Цепочка ретраев длится ~2+4+8+16+32 с; лок с запасом.
_RECONCILE_LOCK_SECONDS = 300


async def _backoff(n: int) -> None:
    # ListQueueBroker не умеет отложенные задачи — ждём внутри воркера перед перепостановкой.
    await asyncio.sleep(get_settings().unknown_poll_base_delay * 2**n)


@broker.task(retry_on_error=True, max_retries=5)
async def process_webhook(event_id: str) -> None:
    await process_event(get_sessionmaker(), get_cascade(), uuid.UUID(event_id))


@broker.task
async def poll_unknown(attempt_id: str, n: int = 0) -> None:
    if await get_cascade().resolve_unknown(uuid.UUID(attempt_id)):
        return
    if n + 1 >= get_settings().unknown_poll_max_attempts:
        logger.error("ALERT attempt %s outcome still unknown — manual review", attempt_id)
        return
    await _backoff(n)
    await poll_unknown.kiq(attempt_id, n + 1)


@broker.task
async def cleanup_orphan(attempt_id: str, n: int = 0) -> None:
    if await get_cascade().cleanup_orphan(uuid.UUID(attempt_id)):
        return
    if n + 1 >= get_settings().unknown_poll_max_attempts:
        logger.error("ALERT orphan link of attempt %s not cancelled — manual review", attempt_id)
        return
    await _backoff(n)
    await cleanup_orphan.kiq(attempt_id, n + 1)


@broker.task
async def refund_duplicate(attempt_id: str, n: int = 0) -> None:
    if await get_cascade().refund_duplicate(uuid.UUID(attempt_id)):
        return
    if n + 1 >= get_settings().unknown_poll_max_attempts:
        logger.error("ALERT duplicate charge of attempt %s not refunded — manual review",
                     attempt_id)
        return
    await _backoff(n)
    await refund_duplicate.kiq(attempt_id, n + 1)


@broker.task
async def resume_payment(payment_id: str) -> None:
    await get_cascade().resume(uuid.UUID(payment_id))


@broker.task
async def sync_attempt(attempt_id: str) -> None:
    await get_cascade().sync_attempt(uuid.UUID(attempt_id))


async def _kick_once(task: AsyncTaskiqDecoratedTask, item_id: uuid.UUID) -> bool:
    """Поставить задачу, если по этому объекту цепочка ещё не запущена. Без этого
    reconcile раз в минуту добавлял бы новую цепочку ретраев к уже работающей."""
    key = f"reconcile:{task.task_name}:{item_id}"
    if not await get_redis().set(key, "1", nx=True, ex=_RECONCILE_LOCK_SECONDS):
        return False
    await task.kiq(str(item_id))
    return True


@broker.task(schedule=[{"cron": "* * * * *"}])
async def reconcile() -> None:
    """Страховочная сетка: подбирает всё, что могли потерять (упал процесс, не дошла задача,
    провайдер не прислал вебхук, клиент бросил 3DS)."""
    settings = get_settings()
    now = datetime.now(UTC)
    stuck_pending = now - timedelta(seconds=settings.provider_read_timeout * 3 + 10)
    stale = now - timedelta(seconds=30)
    stalled_payment = now - timedelta(seconds=60)
    link_expired = now - timedelta(seconds=settings.link_ttl_seconds + 60)
    three_ds_abandoned = now - timedelta(minutes=15)

    async with get_sessionmaker()() as s:
        pending = (await s.scalars(select(PaymentAttempt.id).where(
            PaymentAttempt.status == AttemptStatus.PENDING,
            PaymentAttempt.created_at < stuck_pending))).all()
        unknown = (await s.execute(select(PaymentAttempt.id, PaymentAttempt.stage).where(
            PaymentAttempt.status == AttemptStatus.UNKNOWN,
            PaymentAttempt.updated_at < stale))).all()
        expired_links = (await s.scalars(select(PaymentAttempt.id).where(
            PaymentAttempt.status == AttemptStatus.REQUIRES_ACTION,
            PaymentAttempt.mode == ProviderMode.HOSTED,
            PaymentAttempt.created_at < link_expired))).all()
        abandoned_3ds = (await s.scalars(select(PaymentAttempt.id).where(
            PaymentAttempt.status == AttemptStatus.REQUIRES_ACTION,
            PaymentAttempt.mode == ProviderMode.H2H,
            PaymentAttempt.created_at < three_ds_abandoned))).all()
        duplicates = (await s.scalars(select(PaymentAttempt.id).where(
            PaymentAttempt.status == AttemptStatus.SUCCEEDED,
            PaymentAttempt.is_duplicate.is_(True),
            PaymentAttempt.updated_at < stale))).all()
        # Каскад «потерял» следующий шаг: процесс упал между попытками.
        stalled = (await s.scalars(select(Payment.id).where(
            Payment.status.in_([PaymentStatus.CREATED, PaymentStatus.PROCESSING]),
            Payment.updated_at < stalled_payment))).all()
        webhooks = (await s.scalars(select(WebhookEvent.id).where(
            WebhookEvent.processed_at.is_(None),
            WebhookEvent.received_at < stale,
            WebhookEvent.process_attempts < 10))).all()

    jobs: list[tuple[AsyncTaskiqDecoratedTask, uuid.UUID]] = [
        *((poll_unknown, a) for a in pending),
        *((poll_unknown if stage is Stage.CHARGE else cleanup_orphan, a) for a, stage in unknown),
        *((cleanup_orphan, a) for a in expired_links),
        *((sync_attempt, a) for a in abandoned_3ds),
        *((refund_duplicate, a) for a in duplicates),
        *((resume_payment, p) for p in stalled),
        *((process_webhook, e) for e in webhooks),
    ]
    kicked = await get_cascade().relay_outbox(older_than=stale)  # задачи, не ушедшие в очередь
    for task, item_id in jobs:
        kicked += await _kick_once(task, item_id)
    if kicked:
        logger.info("reconcile: requeued %d items", kicked)


@broker.task(schedule=[{"cron": "17 3 * * *"}])
async def purge_old_data_task() -> None:
    deleted = await purge_old_data(get_sessionmaker(), get_settings())
    logger.info("retention: deleted %s", deleted)

"""Метрики в формате Prometheus, посчитанные по БД.

Считаем по таблицам, а не in-process счётчиками: каскад работает и в API, и в воркерах,
и по БД картина общая. На больших объёмах заменить на счётчики/гистограммы prometheus_client
с multiprocess-режимом — формат ответа менять не придётся.

Типичные запросы:
  approval rate:  sum(casc_attempts{status="succeeded"}) by (provider) / sum(casc_attempts) by (provider)
  доля каскадов:  casc_payments_cascaded / casc_payments_total
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cascade.factory import get_breaker
from app.core.config import get_settings
from app.core.redis import get_redis
from app.db.db import get_session
from app.db.models import OutboxTask, Payment, PaymentAttempt, RoutingRule, WebhookEvent
from app.routing.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ops"])


def _line(name: str, value: float | int, **labels: str) -> str:
    lbl = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}"


async def render_metrics(session: AsyncSession, breaker: CircuitBreaker) -> str:
    out: list[str] = []

    def metric(name: str, help_: str) -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} gauge")

    metric("casc_payments", "Платежи по статусам")
    total = 0
    for status, n in await session.execute(
            select(Payment.status, func.count()).group_by(Payment.status)):
        out.append(_line("casc_payments", n, status=status.value))
        total += n

    metric("casc_payments_total", "Все платежи")
    metric("casc_payments_cascaded", "Платежи, у которых было больше одной попытки")
    out.append(_line("casc_payments_total", total))
    # У каждого платежа с каскадом ровно одна попытка с seq=2 — считаем по ним, без подзапроса
    # на каждый платёж.
    out.append(_line("casc_payments_cascaded", await session.scalar(
        select(func.count()).select_from(PaymentAttempt).where(PaymentAttempt.seq == 2)) or 0))

    metric("casc_attempts", "Попытки по провайдеру и статусу")
    metric("casc_attempt_latency_ms_sum", "Суммарная задержка ответа провайдера, мс")
    metric("casc_attempt_latency_ms_count", "Число попыток с измеренной задержкой")
    for provider, status, n, lat_sum, lat_n in await session.execute(
            select(PaymentAttempt.provider, PaymentAttempt.status, func.count(),
                   func.coalesce(func.sum(PaymentAttempt.latency_ms), 0),
                   func.count(PaymentAttempt.latency_ms))
            .group_by(PaymentAttempt.provider, PaymentAttempt.status)):
        out.append(_line("casc_attempts", n, provider=provider, status=status.value))
        out.append(_line("casc_attempt_latency_ms_sum", lat_sum, provider=provider,
                         status=status.value))
        out.append(_line("casc_attempt_latency_ms_count", lat_n, provider=provider,
                         status=status.value))

    metric("casc_attempts_unknown", "Попытки с неизвестным исходом (нужен разбор)")
    out.append(_line("casc_attempts_unknown", await session.scalar(
        select(func.count()).select_from(PaymentAttempt)
        .where(PaymentAttempt.status == "unknown")) or 0))

    metric("casc_attempts_duplicate", "Двойные списания (попытки с is_duplicate)")
    out.append(_line("casc_attempts_duplicate", await session.scalar(
        select(func.count()).select_from(PaymentAttempt)
        .where(PaymentAttempt.is_duplicate.is_(True))) or 0))

    metric("casc_webhook_inbox_unprocessed", "Необработанные вебхуки")
    out.append(_line("casc_webhook_inbox_unprocessed", await session.scalar(
        select(func.count()).select_from(WebhookEvent)
        .where(WebhookEvent.processed_at.is_(None))) or 0))

    metric("casc_outbox_pending", "Неотправленные фоновые задачи")
    out.append(_line("casc_outbox_pending", await session.scalar(
        select(func.count()).select_from(OutboxTask).where(OutboxTask.sent_at.is_(None))) or 0))

    metric("casc_circuit_open", "1, если circuit breaker провайдера открыт")
    providers = (await session.scalars(select(RoutingRule.provider).distinct())).all()
    for provider in sorted(providers):
        out.append(_line("casc_circuit_open", int(await breaker.is_open(provider)),
                         provider=provider))
    return "\n".join(out) + "\n"


_CACHE_KEY = "metrics:cache"


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(
    session: Annotated[AsyncSession, Depends(get_session)],
    breaker: Annotated[CircuitBreaker, Depends(get_breaker)],
) -> str:
    """Ответ кэшируется в Redis на metrics_cache_seconds: сколько бы ни было скрейперов и
    реплик, агрегирующие запросы к БД идут не чаще одного раза за это время."""
    redis = get_redis()
    try:
        if cached := await redis.get(_CACHE_KEY):
            return cached
    except Exception:
        logger.exception("metrics cache read failed")
    text = await render_metrics(session, breaker)
    try:
        await redis.set(_CACHE_KEY, text, ex=get_settings().metrics_cache_seconds)
    except Exception:
        logger.exception("metrics cache write failed")
    return text

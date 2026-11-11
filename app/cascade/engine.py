"""Ядро каскада.

Правила, ради которых всё написано:
1. Попытка записывается в БД ДО запроса к провайдеру (PENDING) — после падения процесса
   мы знаем, что запрос мог уйти, и reconcile выяснит его судьбу.
2. Во время HTTP-запроса к провайдеру не держим блокировку строки платежа.
   Результат применяется отдельной транзакцией под SELECT ... FOR UPDATE.
3. HARD — стоп, SOFT/CONFIG — следующий провайдер, UNKNOWN на этапе списания — стоп
   до выяснения статуса (иначе можно списать дважды). UNKNOWN на этапе получения ссылки —
   каскадим дальше (денег ещё нет), а возможный «осиротевший» счёт гасим в фоне.
4. Решение «каскадить дальше / завалить платёж» принимает только последняя попытка.
   Старые попытки влияют на платёж только поздним успехом: если деньги всё же прошли —
   либо засчитываем платёж, либо, если он уже оплачен через другую попытку, — авто-возврат.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, auto
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import callback_signature
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
from app.domain.results import CardData, ProviderResult
from app.domain.state_machine import PAYMENT_FINAL, can_attempt, can_payment
from app.providers.base import ProviderAdapter, ProviderRequest, TransportFailure
from app.providers.registry import ProviderRegistry
from app.routing.circuit_breaker import CircuitBreaker
from app.routing.router import RouteStep, build_route, load_rules

logger = logging.getLogger(__name__)


class TaskQueue(Protocol):
    async def poll_unknown(self, attempt_id: uuid.UUID) -> None: ...
    async def cleanup_orphan(self, attempt_id: uuid.UUID) -> None: ...
    async def refund_duplicate(self, attempt_id: uuid.UUID) -> None: ...


class IdempotencyConflict(Exception):
    pass


class NotFound(Exception):
    pass


class InvalidState(Exception):
    pass


class Decision(Enum):
    CONTINUE = auto()  # пробовать следующего провайдера
    STOP = auto()


@dataclass(frozen=True, slots=True)
class CreatePayment:
    idempotency_key: str
    flow: Flow
    order_id: str
    amount: Decimal
    currency: str
    description: str
    method: PaymentMethod
    customer_email: str | None = None
    return_url: str | None = None
    card: CardData | None = None

    def request_hash(self) -> str:
        payload = {
            "flow": self.flow.value, "order_id": self.order_id, "amount": str(self.amount),
            "currency": self.currency, "description": self.description,
            "method": self.method.value, "email": self.customer_email,
            "return_url": self.return_url,
            # PAN не храним даже в хеше целиком — только последние цифры.
            "card": self.card.pan[-4:] if self.card else None,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _AttemptCtx:
    attempt_id: uuid.UUID
    created_at: datetime
    step: RouteStep
    stage: Stage
    request: ProviderRequest


type _Task = Callable[[], Awaitable[None]]


class CascadeEngine:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], registry: ProviderRegistry,
                 breaker: CircuitBreaker, queue: TaskQueue, settings: Settings) -> None:
        self._sm = sessionmaker
        self._registry = registry
        self._breaker = breaker
        self._queue = queue
        self._settings = settings

    # ------------------------------------------------------------------ public API

    async def create_payment(self, cmd: CreatePayment) -> tuple[Payment, bool]:
        """Возвращает (платёж, создан_ли_сейчас). Повтор с тем же Idempotency-Key отдаёт
        существующий платёж без повторного каскада."""
        if (cmd.flow is Flow.CARD) != (cmd.card is not None):
            raise ValueError("card data is required for flow=card and forbidden for flow=link")
        request_hash = cmd.request_hash()
        payment_id = await self._insert_payment(cmd, request_hash)
        if payment_id is None:
            async with self._sm() as s:
                existing = await s.scalar(
                    select(Payment).where(Payment.idempotency_key == cmd.idempotency_key))
            if existing is None:
                raise RuntimeError("payment insert failed for a reason other than idempotency")
            if existing.request_hash != request_hash:
                raise IdempotencyConflict(cmd.idempotency_key)
            return existing, False

        await self.drive(payment_id, card=cmd.card)
        return await self.get_payment(payment_id), True

    async def get_payment(self, payment_id: uuid.UUID) -> Payment:
        async with self._sm() as s:
            payment = await s.get(Payment, payment_id)
            if payment is None:
                raise NotFound(str(payment_id))
            return payment

    async def get_attempt(self, attempt_id: uuid.UUID) -> PaymentAttempt:
        async with self._sm() as s:
            attempt = await s.get(PaymentAttempt, attempt_id)
            if attempt is None:
                raise NotFound(str(attempt_id))
            return attempt

    async def drive(self, payment_id: uuid.UUID, *, card: CardData | None) -> None:
        """Цикл каскада: берём следующий шаг маршрута, пока не будет успеха, HARD-отказа,
        действия клиента или неизвестного исхода списания. Без карты на руках (вызов из
        вебхука/воркера) H2H-шаги пропускаются — остаются только hosted."""
        while True:
            ctx = await self._start_next_attempt(payment_id, have_card=card is not None)
            if ctx is None:
                return
            adapter = self._registry.get(ctx.step.provider)
            started = asyncio.get_running_loop().time()
            try:
                if ctx.step.mode is ProviderMode.H2H:
                    result = await adapter.charge(replace(ctx.request, card=card))
                else:
                    result = await adapter.create_link(ctx.request)
            except Exception as exc:  # неожиданный ответ: запрос ушёл, исход неизвестен
                logger.exception("adapter %s failed", ctx.step.provider)
                result = ProviderResult.unknown("adapter_error", repr(exc))
            latency_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            await self._record_health(ctx.step.provider, result)

            if result.status is AttemptStatus.UNKNOWN and ctx.stage is Stage.CHARGE:
                result = await self._resolve_inline(adapter, ctx, result)

            decision = await self.apply_result(ctx.attempt_id, result, latency_ms=latency_ms)
            if decision is Decision.STOP:
                return

    async def apply_result(self, attempt_id: uuid.UUID, result: ProviderResult, *,
                           latency_ms: int | None = None) -> Decision:
        """Единая точка применения ответа провайдера — синхронного, из вебхука, из
        возврата браузера или из опроса статуса. Идемпотентна."""
        tasks: list[_Task] = []
        async with self._sm() as s, s.begin():
            attempt = await s.get(PaymentAttempt, attempt_id)
            if attempt is None:
                raise NotFound(str(attempt_id))
            payment = await s.get(Payment, attempt.payment_id, with_for_update=True,
                                  populate_existing=True)
            assert payment is not None
            await s.refresh(attempt)  # перечитать уже под блокировкой платежа
            decision = self._apply(payment, attempt, result, latency_ms, tasks)
        await self._dispatch(tasks)
        return decision

    async def apply_external(self, attempt_id: uuid.UUID, result: ProviderResult) -> None:
        """Событие пришло не из цикла drive (вебхук, возврат браузера, опрос, 3DS)."""
        if await self.apply_result(attempt_id, result) is Decision.CONTINUE:
            attempt = await self.get_attempt(attempt_id)
            await self.drive(attempt.payment_id, card=None)

    async def complete_3ds(self, attempt_id: uuid.UUID, params: dict[str, str]) -> Payment:
        attempt = await self.get_attempt(attempt_id)
        if attempt.status is AttemptStatus.REQUIRES_ACTION and attempt.mode is ProviderMode.H2H:
            assert attempt.provider_payment_id is not None
            adapter = self._registry.get(attempt.provider)
            try:
                result = await adapter.complete_3ds(attempt.provider_payment_id, params)
            except Exception as exc:
                logger.exception("complete_3ds %s failed", attempt_id)
                result = ProviderResult.unknown("adapter_error", repr(exc))
            await self.apply_external(attempt_id, result)
        return await self.get_payment(attempt.payment_id)

    async def sync_attempt(self, attempt_id: uuid.UUID, *, strict: bool = False) -> Payment:
        """Сверить статус попытки у провайдера (возврат клиента с hosted-страницы, вебхук).
        Провайдер — источник правды: браузеру и содержимому вебхуков не верим.
        strict=True — если выяснить не удалось, бросить исключение (задача повторится)."""
        attempt = await self.get_attempt(attempt_id)
        if attempt.status in (AttemptStatus.SUCCEEDED, AttemptStatus.REFUNDED):
            return await self.get_payment(attempt.payment_id)
        if attempt.provider_payment_id is None:
            if not await self.resolve_unknown(attempt_id, any_status=True) and strict:
                raise RuntimeError(f"attempt {attempt_id}: provider status unavailable")
            return await self.get_payment(attempt.payment_id)
        adapter = self._registry.get(attempt.provider)
        try:
            result = await adapter.get_status(attempt.provider_payment_id)
        except Exception:
            logger.exception("get_status %s failed", attempt_id)
            result = ProviderResult.unknown("adapter_error")
        if result.technical_failure:
            if strict:
                raise RuntimeError(f"attempt {attempt_id}: provider status unavailable")
        else:
            await self.apply_external(attempt_id, result)
        return await self.get_payment(attempt.payment_id)

    async def resume(self, payment_id: uuid.UUID) -> None:
        """Продолжить каскад, если процесс упал между шагами (платёж завис в CREATED или
        PROCESSING без запроса в полёте). Карты уже нет — только hosted-провайдеры."""
        await self.drive(payment_id, card=None)

    async def resolve_unknown(self, attempt_id: uuid.UUID, *, any_status: bool = False) -> bool:
        """Выяснить исход попытки, по которой не дождались ответа. True — исход известен."""
        attempt = await self.get_attempt(attempt_id)
        if not any_status and attempt.status not in (AttemptStatus.UNKNOWN,
                                                     AttemptStatus.PENDING):
            return True
        adapter = self._registry.get(attempt.provider)
        result = await self._lookup(adapter, attempt.id, attempt.provider_payment_id,
                                    attempt.created_at)
        if result is None or result.status is AttemptStatus.UNKNOWN:
            return False
        await self.apply_external(attempt_id, result)
        return True

    async def cleanup_orphan(self, attempt_id: uuid.UUID) -> bool:
        """Погасить ссылку, которую клиенту не отдали (или отдали, но платёж уже прошёл
        через другую попытку). True — больше делать нечего."""
        attempt = await self.get_attempt(attempt_id)
        if attempt.mode is not ProviderMode.HOSTED or attempt.status not in (
            AttemptStatus.UNKNOWN, AttemptStatus.REQUIRES_ACTION, AttemptStatus.PENDING
        ):
            return True
        adapter = self._registry.get(attempt.provider)
        found = await self._lookup(adapter, attempt.id, attempt.provider_payment_id,
                                   attempt.created_at)
        if found is None:
            return False  # провайдер недоступен или ещё рано делать выводы — повторим
        # Неоплаченный счёт PayMaster отдаёт как Pending (у нас UNKNOWN) — его тоже гасим.
        still_open = found.status in (AttemptStatus.REQUIRES_ACTION, AttemptStatus.UNKNOWN)
        if not still_open:
            # Ссылку уже оплатили / отклонили / её нет — фиксируем факт.
            await self.apply_external(attempt_id, found)
            return True
        ppid = found.provider_payment_id or attempt.provider_payment_id
        if ppid is None or not await adapter.cancel_link(ppid):
            return False
        await self.apply_result(attempt_id, ProviderResult(
            AttemptStatus.CANCELLED, ppid, decline_code="superseded"))
        return True

    async def refund_duplicate(self, attempt_id: uuid.UUID) -> bool:
        attempt = await self.get_attempt(attempt_id)
        if attempt.status is not AttemptStatus.SUCCEEDED or not attempt.is_duplicate:
            return True
        return await self._refund_attempt(attempt)

    async def refund_payment(self, payment_id: uuid.UUID) -> Payment:
        payment = await self.get_payment(payment_id)
        if payment.status is PaymentStatus.REFUNDED:
            return payment
        if payment.status is not PaymentStatus.SUCCEEDED or payment.succeeded_attempt_id is None:
            raise InvalidState(f"payment is {payment.status}")
        attempt = await self.get_attempt(payment.succeeded_attempt_id)
        if not await self._refund_attempt(attempt):
            raise InvalidState("provider rejected refund")
        return await self.get_payment(payment_id)

    # ------------------------------------------------------------------ internals

    async def _insert_payment(self, cmd: CreatePayment, request_hash: str) -> uuid.UUID | None:
        """Создать платёж с маршрутом. None — платёж с таким Idempotency-Key уже есть
        (в т.ч. если параллельный запрос вставил его на долю секунды раньше)."""
        try:
            async with self._sm() as s, s.begin():
                if await s.scalar(select(Payment.id).where(
                        Payment.idempotency_key == cmd.idempotency_key)):
                    return None
                route = build_route(await load_rules(s), flow=cmd.flow, amount=cmd.amount,
                                    currency=cmd.currency, method=cmd.method)
                payment = Payment(
                    flow=cmd.flow, merchant_order_id=cmd.order_id, amount=cmd.amount,
                    currency=cmd.currency, description=cmd.description, method=cmd.method,
                    customer_email=cmd.customer_email, return_url=cmd.return_url,
                    status=PaymentStatus.CREATED, idempotency_key=cmd.idempotency_key,
                    request_hash=request_hash, route_snapshot=[r.to_dict() for r in route],
                    deadline_at=datetime.now(UTC)
                    + timedelta(seconds=self._settings.payment_deadline_seconds),
                )
                s.add(payment)
                await s.flush()
                return payment.id
        except IntegrityError:
            return None

    async def _start_next_attempt(self, payment_id: uuid.UUID, *,
                                  have_card: bool) -> _AttemptCtx | None:
        async with self._sm() as s, s.begin():
            payment = await s.get(Payment, payment_id, with_for_update=True,
                                  populate_existing=True)
            assert payment is not None
            if payment.status in PAYMENT_FINAL or payment.status is PaymentStatus.REQUIRES_ACTION:
                return None
            attempts = list(payment.attempts)
            last = attempts[-1] if attempts else None
            if last is not None and _awaiting_verdict(last):
                return None

            failure_code = last.decline_code if last else None
            if len(attempts) >= self._settings.max_attempts:
                _set_status(payment, PaymentStatus.FAILED, failure_code or "max_attempts")
                return None
            if datetime.now(UTC) >= payment.deadline_at:
                _set_status(payment, PaymentStatus.FAILED, "deadline_exceeded")
                return None

            step = await self._next_step(payment, attempts, have_card)
            if step is None:
                _set_status(payment, PaymentStatus.FAILED, failure_code or "no_route")
                return None

            _set_status(payment, PaymentStatus.PROCESSING)
            stage = Stage.CHARGE if step.mode is ProviderMode.H2H else Stage.LINK
            attempt = PaymentAttempt(
                payment_id=payment.id, seq=len(attempts) + 1, provider=step.provider,
                mode=step.mode, stage=stage, status=AttemptStatus.PENDING,
            )
            s.add(attempt)
            await s.flush()
            await s.refresh(attempt, ["created_at"])
            base = self._settings.public_base_url
            request = ProviderRequest(
                attempt_id=str(attempt.id),
                idempotency_key=f"{payment.id}:{attempt.seq}",
                amount=payment.amount, currency=payment.currency,
                description=payment.description, method=payment.method,
                customer_email=payment.customer_email,
                return_url=f"{base}/v1/return/{attempt.id}",
                callback_url=(f"{base}/webhooks/paymaster/{step.provider}/{attempt.id}/"
                              f"{callback_signature(step.provider, str(attempt.id))}"),
                link_ttl_seconds=self._settings.link_ttl_seconds,
            )
            logger.info("payment %s attempt #%d → %s (%s)", payment.id, attempt.seq,
                        step.provider, step.mode)
            return _AttemptCtx(attempt.id, attempt.created_at, step, stage, request)

    async def _next_step(self, payment: Payment, attempts: list[PaymentAttempt],
                         have_card: bool) -> RouteStep | None:
        tried = {(a.provider, a.mode) for a in attempts}
        for raw in payment.route_snapshot:
            step = RouteStep.from_dict(raw)
            if (step.provider, step.mode) in tried:
                continue
            if step.mode is ProviderMode.H2H and not have_card:
                continue
            if await self._circuit_open(step.provider):
                logger.warning("payment %s: skip %s — circuit open", payment.id, step.provider)
                continue
            return step
        return None

    def _apply(self, payment: Payment, attempt: PaymentAttempt, result: ProviderResult,
               latency_ms: int | None, tasks: list[_Task]) -> Decision:
        prev = attempt.status
        new = result.status
        if new is prev:
            return Decision.STOP  # повторное событие
        hosted_pending = (attempt.mode is ProviderMode.HOSTED
                          and prev is AttemptStatus.REQUIRES_ACTION and new is AttemptStatus.UNKNOWN)
        if hosted_pending or not can_attempt(prev, new):
            logger.info("attempt %s: ignore %s → %s", attempt.id, prev, new)
            return Decision.STOP

        attempt.status = new
        if attempt.provider_payment_id is None:
            attempt.provider_payment_id = result.provider_payment_id
        if new in (AttemptStatus.DECLINED, AttemptStatus.ERROR, AttemptStatus.UNKNOWN,
                   AttemptStatus.CANCELLED):
            attempt.decline_code = result.decline_code
            attempt.decline_class = result.decline_class
            attempt.message = (result.message or "")[:512] or None
        if result.next_action is not None:
            attempt.next_action = result.next_action.to_dict()
        if result.raw:
            attempt.raw_response = result.raw
        if latency_ms is not None:
            attempt.latency_ms = latency_ms

        is_latest = attempt.seq == max(a.seq for a in payment.attempts)

        if new is AttemptStatus.SUCCEEDED:
            self._on_success(payment, attempt, tasks)
            return Decision.STOP

        if not is_latest or payment.status in PAYMENT_FINAL:
            # Старая попытка (напр. погашенная ссылка отклонена) на платёж не влияет.
            return Decision.STOP

        match new:
            case AttemptStatus.REQUIRES_ACTION:
                _set_status(payment, PaymentStatus.REQUIRES_ACTION)
                return Decision.STOP

            case AttemptStatus.UNKNOWN if attempt.stage is Stage.CHARGE:
                logger.warning("attempt %s: outcome unknown — no cascade until resolved",
                               attempt.id)
                _set_status(payment, PaymentStatus.PROCESSING)
                tasks.append(lambda: self._queue.poll_unknown(attempt.id))
                return Decision.STOP

            case AttemptStatus.UNKNOWN:  # stage LINK: денег нет, каскадим, счёт гасим в фоне
                tasks.append(lambda: self._queue.cleanup_orphan(attempt.id))
                return self._continue(payment)

            case AttemptStatus.CANCELLED:  # ссылка, которую видел клиент, истекла
                _set_status(payment, PaymentStatus.FAILED, result.decline_code or "cancelled")
                return Decision.STOP

            case AttemptStatus.DECLINED | AttemptStatus.ERROR:
                cls = result.decline_class or DeclineClass.SOFT
                customer_saw_page = (attempt.mode is ProviderMode.HOSTED
                                     and prev is AttemptStatus.REQUIRES_ACTION)
                if cls is DeclineClass.HARD or (
                    customer_saw_page and not self._settings.cascade_after_hosted_decline
                ):
                    _set_status(payment, PaymentStatus.FAILED, result.decline_code)
                    return Decision.STOP
                if cls is DeclineClass.CONFIG:
                    logger.error("ALERT provider %s misconfigured: %s %s", attempt.provider,
                                 result.decline_code, result.message)
                return self._continue(payment)

        return Decision.STOP

    @staticmethod
    def _continue(payment: Payment) -> Decision:
        # Платёж мог ждать действия клиента по этой попытке (3DS) — возвращаем в работу.
        _set_status(payment, PaymentStatus.PROCESSING)
        return Decision.CONTINUE

    def _on_success(self, payment: Payment, attempt: PaymentAttempt, tasks: list[_Task]) -> None:
        if payment.succeeded_attempt_id is None:
            payment.succeeded_attempt_id = attempt.id
            _set_status(payment, PaymentStatus.SUCCEEDED)
            payment.failure_code = None
            # Висящие ссылки других попыток больше не нужны: гасим, чтобы клиент не оплатил дважды.
            for other in payment.attempts:
                if other.id != attempt.id and other.mode is ProviderMode.HOSTED and other.status in (
                    AttemptStatus.REQUIRES_ACTION, AttemptStatus.UNKNOWN
                ):
                    tasks.append(lambda oid=other.id: self._queue.cleanup_orphan(oid))
        elif payment.succeeded_attempt_id != attempt.id:
            attempt.is_duplicate = True
            logger.error("ALERT double charge: payment %s already paid by %s, attempt %s also "
                         "succeeded — scheduling refund", payment.id,
                         payment.succeeded_attempt_id, attempt.id)
            tasks.append(lambda: self._queue.refund_duplicate(attempt.id))

    async def _resolve_inline(self, adapter: ProviderAdapter, ctx: _AttemptCtx,
                              result: ProviderResult) -> ProviderResult:
        """Пока клиент ждёт ответа, пару раз спрашиваем провайдера о судьбе запроса."""
        for _ in range(self._settings.unknown_inline_polls):
            await asyncio.sleep(self._settings.unknown_inline_poll_interval)
            found = await self._lookup(adapter, ctx.attempt_id, result.provider_payment_id,
                                       ctx.created_at)
            if found is not None and found.status is not AttemptStatus.UNKNOWN:
                return found
        return result

    async def _lookup(self, adapter: ProviderAdapter, attempt_id: uuid.UUID,
                      provider_payment_id: str | None,
                      created_at: datetime) -> ProviderResult | None:
        """Статус операции у провайдера. None — пока не можем сказать ничего определённого."""
        try:
            if provider_payment_id:
                found = await adapter.get_status(provider_payment_id)
                return None if found.technical_failure else found
            found = await adapter.find_by_order(str(attempt_id), created_at)
        except TransportFailure as f:  # провайдер недоступен — ожидаемо, повторим позже
            logger.warning("lookup %s at %s failed: %s", attempt_id, adapter.name,
                           f.result.decline_code)
            return None
        except Exception:
            logger.exception("lookup %s at %s failed", attempt_id, adapter.name)
            return None
        if found is not None:
            return found
        age = (datetime.now(UTC) - created_at).total_seconds()
        if age < self._settings.unknown_not_found_grace_seconds:
            return None
        # Провайдер не знает такой операции — запрос до него не дошёл. Безопасно каскадить.
        return ProviderResult.error("not_created", DeclineClass.SOFT,
                                    "provider has no operation for this attempt")

    async def _refund_attempt(self, attempt: PaymentAttempt) -> bool:
        assert attempt.provider_payment_id is not None
        payment = await self.get_payment(attempt.payment_id)
        adapter = self._registry.get(attempt.provider)
        refund = await adapter.refund(attempt.provider_payment_id, payment.amount,
                                      payment.currency, f"refund:{attempt.id}")
        if not refund.ok:
            logger.error("refund of attempt %s failed: %s", attempt.id, refund.message)
            return False
        async with self._sm() as s, s.begin():
            p = await s.get(Payment, attempt.payment_id, with_for_update=True,
                            populate_existing=True)
            a = await s.get(PaymentAttempt, attempt.id, populate_existing=True)
            assert p is not None and a is not None
            if can_attempt(a.status, AttemptStatus.REFUNDED):
                a.status = AttemptStatus.REFUNDED
            if p.succeeded_attempt_id == a.id:
                _set_status(p, PaymentStatus.REFUNDED)
        return True

    async def _circuit_open(self, provider: str) -> bool:
        try:
            return await self._breaker.is_open(provider)
        except Exception:  # недоступный Redis не должен останавливать платежи
            logger.exception("circuit breaker check failed — treating %s as closed", provider)
            return False

    async def _record_health(self, provider: str, result: ProviderResult) -> None:
        try:
            if result.technical_failure:
                await self._breaker.record_failure(provider)
            else:
                await self._breaker.record_success(provider)
        except Exception:  # недоступный Redis не должен ронять платёж
            logger.exception("circuit breaker update failed")

    @staticmethod
    async def _dispatch(tasks: list[_Task]) -> None:
        for task in tasks:
            try:
                await task()
            except Exception:  # reconcile подберёт пропущенное
                logger.exception("failed to enqueue background task")


def _set_status(payment: Payment, dst: PaymentStatus, failure_code: str | None = None) -> None:
    if payment.status is dst:
        return
    if not can_payment(payment.status, dst):
        logger.info("payment %s: ignore %s → %s", payment.id, payment.status, dst)
        return
    payment.status = dst
    if dst is PaymentStatus.FAILED:
        payment.failure_code = failure_code


def _awaiting_verdict(attempt: PaymentAttempt) -> bool:
    """По попытке ещё нет вердикта: запрос в полёте, клиент на 3DS/странице оплаты,
    или списание с неизвестным исходом. Следующую попытку начинать нельзя."""
    if attempt.status in (AttemptStatus.PENDING, AttemptStatus.REQUIRES_ACTION):
        return True
    return attempt.status is AttemptStatus.UNKNOWN and attempt.stage is Stage.CHARGE

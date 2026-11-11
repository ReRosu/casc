import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cascade.engine import CreatePayment
from app.cascade.inbox import process_event, store_event
from app.db.models import WebhookEvent
from app.domain.enums import AttemptStatus, Flow, PaymentMethod, PaymentStatus
from app.domain.results import NextAction, ProviderResult
from tests.integration.conftest import World


async def _link_payment(world: World) -> tuple[uuid.UUID, uuid.UUID]:
    world.pally.link_results = [ProviderResult(AttemptStatus.REQUIRES_ACTION, "bill-1",
                                               next_action=NextAction("redirect", "https://p"))]
    payment, _ = await world.engine.create_payment(CreatePayment(
        idempotency_key="key-inbox", flow=Flow.LINK, order_id="o", amount=Decimal("100.00"),
        currency="RUB", description="d", method=PaymentMethod.SBP))
    return payment.id, payment.attempts[0].id


async def test_duplicate_webhook_is_stored_once(sm: async_sessionmaker[AsyncSession]) -> None:
    first = await store_event(sm, "pally", "bill-1:SUCCESS", {"a": 1}, True)
    second = await store_event(sm, "pally", "bill-1:SUCCESS", {"a": 1}, True)
    other_status = await store_event(sm, "pally", "bill-1:FAIL", {"a": 1}, True)

    assert first is not None
    assert second is None
    assert other_status is not None


async def test_pally_postback_marks_payment_succeeded(
    sm: async_sessionmaker[AsyncSession], world: World
) -> None:
    payment_id, attempt_id = await _link_payment(world)
    world.pally.status_result = ProviderResult(AttemptStatus.SUCCEEDED, "bill-1")
    payload = {"InvId": str(attempt_id), "OutSum": "100.00", "TrsId": "bill-1", "Status": "SUCCESS",
               "CurrencyIn": "RUB"}
    event_id = await store_event(sm, "pally", "bill-1:SUCCESS", payload, True)
    assert event_id is not None

    await process_event(sm, world.engine, event_id)
    await process_event(sm, world.engine, event_id)  # повторная обработка — no-op

    payment = await world.engine.get_payment(payment_id)
    assert payment.status is PaymentStatus.SUCCEEDED
    async with sm() as s:
        event = await s.get(WebhookEvent, event_id)
        assert event is not None and event.processed_at is not None
        assert event.process_attempts == 1


async def test_webhook_status_is_confirmed_via_provider_api(
    sm: async_sessionmaker[AsyncSession], world: World
) -> None:
    """Postback говорит SUCCESS, а провайдер — счёт ещё не оплачен: верим провайдеру."""
    payment_id, attempt_id = await _link_payment(world)
    world.pally.status_result = ProviderResult(AttemptStatus.REQUIRES_ACTION, "bill-1")
    event_id = await store_event(sm, "pally", "x:SUCCESS",
                                 {"InvId": str(attempt_id), "Status": "SUCCESS"}, True)
    assert event_id is not None

    await process_event(sm, world.engine, event_id)

    payment = await world.engine.get_payment(payment_id)
    assert payment.status is PaymentStatus.REQUIRES_ACTION


async def test_webhook_retried_when_provider_unavailable(
    sm: async_sessionmaker[AsyncSession], world: World
) -> None:
    _, attempt_id = await _link_payment(world)
    world.pally.status_result = ProviderResult.unknown("timeout")
    event_id = await store_event(sm, "pally", "y:SUCCESS",
                                 {"InvId": str(attempt_id), "Status": "SUCCESS"}, True)
    assert event_id is not None

    with pytest.raises(RuntimeError):
        await process_event(sm, world.engine, event_id)

    async with sm() as s:
        event = await s.get(WebhookEvent, event_id)
        assert event is not None and event.processed_at is None  # reconcile подберёт

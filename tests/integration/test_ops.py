"""Outbox, лимит по карте, метрики, чистка."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.metrics import render_metrics
from app.cascade.engine import CardVelocityExceeded, CreatePayment
from app.cascade.inbox import store_event
from app.cascade.retention import purge_old_data
from app.db.models import OutboxTask, WebhookEvent
from app.domain.enums import AttemptStatus, DeclineClass, Flow, PaymentMethod
from app.domain.results import CardData, ProviderResult
from tests.integration.conftest import World, make_world, ok
from tests.integration.test_engine import card_cmd, declined, link, link_cmd


async def _unsent(sm: async_sessionmaker[AsyncSession]) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(OutboxTask)
                              .where(OutboxTask.sent_at.is_(None))) or 0


# ---------------------------------------------------------------- outbox


async def test_outbox_task_is_sent_after_commit(sm: async_sessionmaker[AsyncSession],
                                                world: World) -> None:
    world.a.charge_results = [ProviderResult.unknown("timeout")]
    world.a.find_result = ProviderResult(AttemptStatus.UNKNOWN, None)

    payment, _ = await world.engine.create_payment(card_cmd())

    assert world.queue.polls == [payment.attempts[0].id]
    assert await _unsent(sm) == 0


async def test_outbox_keeps_task_when_queue_is_down_and_reconcile_resends(
    sm: async_sessionmaker[AsyncSession], world: World
) -> None:
    async def down(attempt_id) -> None:  # type: ignore[no-untyped-def]
        raise ConnectionError("redis down")

    world.queue.poll_unknown = down  # type: ignore[method-assign]
    world.a.charge_results = [ProviderResult.unknown("timeout")]
    world.a.find_result = ProviderResult(AttemptStatus.UNKNOWN, None)
    payment, _ = await world.engine.create_payment(card_cmd())
    assert await _unsent(sm) == 1  # задача не потеряна, лежит в БД

    del world.queue.poll_unknown  # очередь вернулась
    resent = await world.engine.relay_outbox(older_than=datetime.now(UTC) + timedelta(seconds=1))

    assert resent == 1
    assert world.queue.polls == [payment.attempts[0].id]
    assert await _unsent(sm) == 0


async def test_outbox_row_is_written_in_same_transaction_as_state(
    sm: async_sessionmaker[AsyncSession], world: World
) -> None:
    world.pally.link_results = [ProviderResult.unknown("timeout")]
    world.a.link_results = [link("pm-1")]

    payment, _ = await world.engine.create_payment(link_cmd())

    async with sm() as s:
        kinds = list((await s.scalars(select(OutboxTask.kind))).all())
    assert kinds == ["cleanup_orphan"]
    assert payment.attempts[0].status is AttemptStatus.UNKNOWN


# ---------------------------------------------------------------- лимит по карте


async def test_card_velocity_limit(sm: async_sessionmaker[AsyncSession]) -> None:
    w = make_world(sm, card_velocity_limit=2)
    w.a.charge_results = [ok("a-1"), ok("a-2"), ok("a-3")]

    await w.engine.create_payment(card_cmd(key="vel-key-1"))
    await w.engine.create_payment(card_cmd(key="vel-key-2"))
    with pytest.raises(CardVelocityExceeded):
        await w.engine.create_payment(card_cmd(key="vel-key-3"))

    assert w.a.calls == ["charge", "charge"]  # третий платёж до провайдера не дошёл


async def test_card_velocity_ignores_idempotent_replay_and_other_cards(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    w = make_world(sm, card_velocity_limit=1)
    w.a.charge_results = [ok("a-1"), ok("a-2")]
    cmd = card_cmd(key="vel-key-1")

    await w.engine.create_payment(cmd)
    _, created = await w.engine.create_payment(cmd)  # повтор — не новая попытка по карте
    other = CreatePayment(idempotency_key="vel-key-2", flow=Flow.CARD, order_id="o",
                          amount=Decimal("10.00"), currency="RUB", description="d",
                          method=PaymentMethod.BANKCARD,
                          card=CardData("5555555555554444", "12/29", "123"))
    await w.engine.create_payment(other)  # другая карта

    assert not created


async def test_pan_is_not_stored(sm: async_sessionmaker[AsyncSession], world: World) -> None:
    world.a.charge_results = [ok("a-1")]
    await world.engine.create_payment(card_cmd())

    async with sm() as s:
        dump = (await s.execute(text(
            "select row_to_json(p)::text || coalesce((select string_agg(row_to_json(a)::text, '') "
            "from payment_attempts a), '') from payments p"))).scalar_one()
    assert "4111111111111111" not in dump


# ---------------------------------------------------------------- метрики


async def test_metrics_render(sm: async_sessionmaker[AsyncSession], world: World) -> None:
    world.a.charge_results = [declined("TransactionDeclined", DeclineClass.SOFT)]
    world.b.charge_results = [ok("b-1")]
    await world.engine.create_payment(card_cmd())
    world.a.charge_results = [ok("a-2")]  # второй платёж — без каскада
    await world.engine.create_payment(card_cmd(key="key-card-2"))
    await world.breaker.force_open("pally", 30)

    async with sm() as s:
        text_ = await render_metrics(s, world.breaker)

    assert 'casc_payments{status="succeeded"} 2' in text_
    assert "casc_payments_total 2" in text_
    assert "casc_payments_cascaded 1" in text_
    assert 'casc_attempts{provider="paymaster_a",status="declined"} 1' in text_
    assert 'casc_attempts{provider="paymaster_a",status="succeeded"} 1' in text_
    assert 'casc_attempts{provider="paymaster_b",status="succeeded"} 1' in text_
    assert 'casc_circuit_open{provider="pally"} 1' in text_
    assert 'casc_circuit_open{provider="paymaster_a"} 0' in text_
    assert "casc_attempts_duplicate 0" in text_
    assert "casc_outbox_pending 0" in text_


# ---------------------------------------------------------------- чистка


async def test_purge_old_data(sm: async_sessionmaker[AsyncSession], world: World) -> None:
    old_id = await store_event(sm, "pally", "old:SUCCESS", {"a": 1}, True)
    fresh_id = await store_event(sm, "pally", "fresh:SUCCESS", {"a": 1}, True)
    unprocessed_id = await store_event(sm, "pally", "stuck:SUCCESS", {"a": 1}, True)
    long_ago = datetime.now(UTC) - timedelta(days=60)
    async with sm() as s, s.begin():
        await s.execute(update(WebhookEvent).where(WebhookEvent.id == old_id)
                        .values(processed_at=long_ago))
        await s.execute(update(WebhookEvent).where(WebhookEvent.id == fresh_id)
                        .values(processed_at=datetime.now(UTC)))
        # необработанное не удаляем, даже если старое
        await s.execute(update(WebhookEvent).where(WebhookEvent.id == unprocessed_id)
                        .values(received_at=long_ago))

    deleted = await purge_old_data(sm, world.settings)

    assert deleted["webhook_inbox"] == 1
    async with sm() as s:
        left = set((await s.scalars(select(WebhookEvent.id))).all())
    assert left == {fresh_id, unprocessed_id}

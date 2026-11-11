import asyncio
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cascade.engine import CreatePayment, IdempotencyConflict
from app.domain.enums import (
    AttemptStatus,
    DeclineClass,
    Flow,
    PaymentMethod,
    PaymentStatus,
    ProviderMode,
)
from app.domain.results import CardData, NextAction, ProviderResult
from tests.integration.conftest import World, make_world, ok

CARD = CardData("4111111111111111", "12/29", "123")


def card_cmd(key: str = "key-card-1", amount: str = "1500.00") -> CreatePayment:
    return CreatePayment(idempotency_key=key, flow=Flow.CARD, order_id="o-1",
                         amount=Decimal(amount), currency="RUB", description="test",
                         method=PaymentMethod.BANKCARD, card=CARD)


def link_cmd(key: str = "key-link-1") -> CreatePayment:
    return CreatePayment(idempotency_key=key, flow=Flow.LINK, order_id="o-2",
                         amount=Decimal("1000.00"), currency="RUB", description="test",
                         method=PaymentMethod.SBP)


def declined(code: str, cls: DeclineClass, ppid: str = "x") -> ProviderResult:
    return ProviderResult(AttemptStatus.DECLINED, ppid, decline_code=code, decline_class=cls)


def link(ppid: str) -> ProviderResult:
    return ProviderResult(AttemptStatus.REQUIRES_ACTION, ppid,
                          next_action=NextAction("redirect", f"https://pay/{ppid}"))


def statuses(payment) -> list[tuple[str, AttemptStatus]]:  # type: ignore[no-untyped-def]
    return [(a.provider, a.status) for a in payment.attempts]


# ---------------------------------------------------------------- flow=card


async def test_soft_decline_cascades_to_next_gateway(world: World) -> None:
    world.a.charge_results = [declined("TransactionDeclined", DeclineClass.SOFT)]
    world.b.charge_results = [ok("b-1")]

    payment, created = await world.engine.create_payment(card_cmd())

    assert created
    assert payment.status is PaymentStatus.SUCCEEDED
    assert statuses(payment) == [("paymaster_a", AttemptStatus.DECLINED),
                                 ("paymaster_b", AttemptStatus.SUCCEEDED)]
    assert payment.succeeded_attempt_id == payment.attempts[1].id


async def test_hard_decline_stops_cascade(world: World) -> None:
    world.a.charge_results = [declined("InsufficientFunds", DeclineClass.HARD)]

    payment, _ = await world.engine.create_payment(card_cmd())

    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_code == "InsufficientFunds"
    assert world.b.calls == []  # к другому шлюзу не ходили


async def test_all_gateways_soft_decline_fallback_to_hosted(world: World) -> None:
    world.a.charge_results = [declined("TransactionDeclined", DeclineClass.SOFT)]
    world.b.charge_results = [declined("IssuerUnavailable", DeclineClass.SOFT)]
    world.pally.link_results = [link("bill-1")]

    payment, _ = await world.engine.create_payment(card_cmd())

    assert payment.status is PaymentStatus.REQUIRES_ACTION
    assert payment.attempts[-1].provider == "pally"
    assert payment.attempts[-1].mode is ProviderMode.HOSTED
    assert payment.attempts[-1].next_action == {
        "type": "redirect", "url": "https://pay/bill-1", "method": "GET", "params": {}}


async def test_config_error_skips_provider(world: World) -> None:
    world.a.charge_results = [ProviderResult.error("not_authorized", DeclineClass.CONFIG)]
    world.b.charge_results = [ok("b-1")]

    payment, _ = await world.engine.create_payment(card_cmd())

    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.attempts[0].decline_class is DeclineClass.CONFIG


async def test_max_attempts_limits_cascade(sm: async_sessionmaker[AsyncSession]) -> None:
    w = make_world(sm, max_attempts=2)
    w.a.charge_results = [declined("TransactionDeclined", DeclineClass.SOFT)]
    w.b.charge_results = [declined("TransactionDeclined", DeclineClass.SOFT)]

    payment, _ = await w.engine.create_payment(card_cmd())

    assert payment.status is PaymentStatus.FAILED
    assert len(payment.attempts) == 2
    assert w.pally.calls == []


async def test_open_circuit_skips_provider_without_attempt(world: World) -> None:
    await world.breaker.force_open("paymaster_a", 30)
    world.b.charge_results = [ok("b-1")]

    payment, _ = await world.engine.create_payment(card_cmd())

    assert statuses(payment) == [("paymaster_b", AttemptStatus.SUCCEEDED)]
    assert world.a.calls == []


async def test_technical_failures_open_circuit(world: World) -> None:
    for i in range(3):
        world.a.charge_results = [ProviderResult.error("http_500", DeclineClass.SOFT,
                                                       technical=True)]
        world.b.charge_results = [ok(f"b-{i}")]
        await world.engine.create_payment(card_cmd(key=f"key-cb-{i}"))

    assert await world.breaker.is_open("paymaster_a")


# ---------------------------------------------------------------- 3DS


async def test_3ds_success(world: World) -> None:
    world.a.charge_results = [ProviderResult(
        AttemptStatus.REQUIRES_ACTION, "a-1",
        next_action=NextAction("3ds", "https://acs", "POST", {"PaReq": "x"}))]
    world.a.complete_results = [ok("a-1")]

    payment, _ = await world.engine.create_payment(card_cmd())
    assert payment.status is PaymentStatus.REQUIRES_ACTION

    payment = await world.engine.complete_3ds(payment.attempts[0].id, {"PaRes": "ok"})
    assert payment.status is PaymentStatus.SUCCEEDED


async def test_soft_decline_after_3ds_continues_only_to_hosted(world: World) -> None:
    """После 3DS карты у нас уже нет — H2H-шлюз B пропускается, идём на hosted."""
    world.a.charge_results = [ProviderResult(AttemptStatus.REQUIRES_ACTION, "a-1",
                                             next_action=NextAction("3ds", "https://acs"))]
    world.a.complete_results = [declined("TransactionDeclined", DeclineClass.SOFT, "a-1")]
    world.pally.link_results = [link("bill-1")]

    payment, _ = await world.engine.create_payment(card_cmd())
    payment = await world.engine.complete_3ds(payment.attempts[0].id, {"PaRes": "ok"})

    assert world.b.calls == []
    assert payment.status is PaymentStatus.REQUIRES_ACTION
    assert statuses(payment) == [("paymaster_a", AttemptStatus.DECLINED),
                                 ("pally", AttemptStatus.REQUIRES_ACTION)]


async def test_3ds_failed_is_hard(world: World) -> None:
    world.a.charge_results = [ProviderResult(AttemptStatus.REQUIRES_ACTION, "a-1",
                                             next_action=NextAction("3ds", "https://acs"))]
    world.a.complete_results = [declined("ThreeDSecureFailed", DeclineClass.HARD, "a-1")]

    payment, _ = await world.engine.create_payment(card_cmd())
    payment = await world.engine.complete_3ds(payment.attempts[0].id, {"PaRes": "fail"})

    assert payment.status is PaymentStatus.FAILED
    assert world.pally.calls == []


# ---------------------------------------------------------------- UNKNOWN / двойное списание


async def test_timeout_on_charge_does_not_cascade(world: World) -> None:
    world.a.charge_results = [ProviderResult.unknown("timeout")]
    world.a.find_result = ProviderResult(AttemptStatus.UNKNOWN, "a-1")  # провайдер: «в процессе»

    payment, _ = await world.engine.create_payment(card_cmd())

    assert payment.status is PaymentStatus.PROCESSING
    assert statuses(payment) == [("paymaster_a", AttemptStatus.UNKNOWN)]
    assert world.b.calls == []  # главное: второй раз не списываем
    assert world.queue.polls == [payment.attempts[0].id]


async def test_timeout_resolved_inline_as_charged(world: World) -> None:
    world.a.charge_results = [ProviderResult.unknown("timeout")]
    world.a.find_result = ok("a-1")

    payment, _ = await world.engine.create_payment(card_cmd())

    assert payment.status is PaymentStatus.SUCCEEDED
    assert world.b.calls == []


async def test_timeout_resolved_inline_as_not_created_cascades(world: World) -> None:
    world.a.charge_results = [ProviderResult.unknown("timeout")]
    world.a.find_result = None  # провайдер такой операции не знает
    world.b.charge_results = [ok("b-1")]

    payment, _ = await world.engine.create_payment(card_cmd())

    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.attempts[0].decline_code == "not_created"


async def test_background_resolution_not_created_continues_without_card(world: World) -> None:
    world.a.charge_results = [ProviderResult.unknown("timeout")]
    world.a.find_result = ProviderResult(AttemptStatus.UNKNOWN, None)
    payment, _ = await world.engine.create_payment(card_cmd())
    assert payment.status is PaymentStatus.PROCESSING

    # Позже воркер выясняет: операции нет. Карты уже нет — только hosted-резерв.
    world.a.find_result = None
    world.pally.link_results = [link("bill-1")]
    assert await world.engine.resolve_unknown(world.queue.polls[0])

    payment = await world.engine.get_payment(payment.id)
    assert world.b.calls == []
    assert statuses(payment) == [("paymaster_a", AttemptStatus.ERROR),
                                 ("pally", AttemptStatus.REQUIRES_ACTION)]


# ---------------------------------------------------------------- flow=link


async def test_link_soft_error_cascades(world: World) -> None:
    world.pally.link_results = [ProviderResult.error("api:error.rate-not-found",
                                                     DeclineClass.SOFT)]
    world.a.link_results = [link("pm-1")]

    payment, _ = await world.engine.create_payment(link_cmd())

    assert payment.status is PaymentStatus.REQUIRES_ACTION
    assert statuses(payment) == [("pally", AttemptStatus.ERROR),
                                 ("paymaster_a", AttemptStatus.REQUIRES_ACTION)]


async def test_link_hard_error_returns_immediately(world: World) -> None:
    world.pally.link_results = [ProviderResult.error("api:error.invalid_amount",
                                                     DeclineClass.HARD)]

    payment, _ = await world.engine.create_payment(link_cmd())

    assert payment.status is PaymentStatus.FAILED
    assert world.a.calls == []


async def test_link_timeout_cascades_and_cancels_orphan(world: World) -> None:
    world.pally.link_results = [ProviderResult.unknown("timeout")]
    world.a.link_results = [link("pm-1")]

    payment, _ = await world.engine.create_payment(link_cmd())

    # На этапе ссылки денег нет — каскадим сразу, не дожидаясь выяснения.
    assert payment.status is PaymentStatus.REQUIRES_ACTION
    orphan_id = payment.attempts[0].id
    assert world.queue.orphans == [orphan_id]

    # Воркер находит созданный-таки счёт и гасит его.
    world.pally.find_result = link("bill-orphan")
    assert await world.engine.cleanup_orphan(orphan_id)
    assert world.pally.cancelled == ["bill-orphan"]
    payment = await world.engine.get_payment(payment.id)
    assert payment.attempts[0].status is AttemptStatus.CANCELLED
    assert payment.status is PaymentStatus.REQUIRES_ACTION  # клиент платит по второй ссылке


async def test_hosted_decline_does_not_cascade_by_default(world: World) -> None:
    world.pally.link_results = [link("bill-1")]
    payment, _ = await world.engine.create_payment(link_cmd())

    await world.engine.apply_external(payment.attempts[0].id,
                                      declined("FAIL", DeclineClass.SOFT, "bill-1"))

    payment = await world.engine.get_payment(payment.id)
    assert payment.status is PaymentStatus.FAILED
    assert world.a.calls == []


async def test_hosted_decline_cascades_when_enabled(sm: async_sessionmaker[AsyncSession]) -> None:
    w = make_world(sm, cascade_after_hosted_decline=True)
    w.pally.link_results = [link("bill-1")]
    w.a.link_results = [link("pm-1")]
    payment, _ = await w.engine.create_payment(link_cmd())

    await w.engine.apply_external(payment.attempts[0].id,
                                  declined("FAIL", DeclineClass.SOFT, "bill-1"))

    payment = await w.engine.get_payment(payment.id)
    assert payment.status is PaymentStatus.REQUIRES_ACTION
    assert payment.attempts[-1].provider == "paymaster_a"


# ---------------------------------------------------------------- поздний успех


async def test_late_success_of_orphan_triggers_refund(world: World) -> None:
    world.pally.link_results = [ProviderResult.unknown("timeout")]
    world.a.link_results = [link("pm-1")]
    payment, _ = await world.engine.create_payment(link_cmd())
    orphan, current = payment.attempts

    # Клиент оплатил вторую ссылку…
    await world.engine.apply_external(current.id, ok("pm-1"))
    # …а потом пришёл успех и по «осиротевшему» счёту: деньги списаны дважды.
    await world.engine.apply_external(orphan.id, ok("bill-orphan"))

    payment = await world.engine.get_payment(payment.id)
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.succeeded_attempt_id == current.id
    assert payment.attempts[0].is_duplicate
    assert world.queue.refunds == [orphan.id]

    assert await world.engine.refund_duplicate(orphan.id)
    assert world.pally.refunded == ["bill-orphan"]
    payment = await world.engine.get_payment(payment.id)
    assert payment.attempts[0].status is AttemptStatus.REFUNDED
    assert payment.status is PaymentStatus.SUCCEEDED  # основной платёж не тронут


async def test_late_success_revives_failed_payment(world: World) -> None:
    world.a.charge_results = [ProviderResult.unknown("timeout")]
    world.a.find_result = ProviderResult(AttemptStatus.UNKNOWN, None)
    payment, _ = await world.engine.create_payment(card_cmd())
    attempt_id = payment.attempts[0].id

    world.a.find_result = ok("a-1")
    assert await world.engine.resolve_unknown(attempt_id)

    payment = await world.engine.get_payment(payment.id)
    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.succeeded_attempt_id == attempt_id


async def test_stale_decline_of_old_attempt_is_ignored(world: World) -> None:
    world.pally.link_results = [ProviderResult.unknown("timeout")]
    world.a.link_results = [link("pm-1")]
    payment, _ = await world.engine.create_payment(link_cmd())

    await world.engine.apply_external(payment.attempts[0].id,
                                      declined("FAIL", DeclineClass.SOFT, "bill-orphan"))

    payment = await world.engine.get_payment(payment.id)
    assert payment.status is PaymentStatus.REQUIRES_ACTION  # решает только последняя попытка


async def test_duplicate_events_are_idempotent(world: World) -> None:
    world.pally.link_results = [link("bill-1")]
    payment, _ = await world.engine.create_payment(link_cmd())
    attempt_id = payment.attempts[0].id

    for _ in range(3):
        await world.engine.apply_external(attempt_id, ok("bill-1"))

    payment = await world.engine.get_payment(payment.id)
    assert payment.status is PaymentStatus.SUCCEEDED
    assert world.queue.refunds == []


# ---------------------------------------------------------------- идемпотентность


async def test_same_idempotency_key_returns_same_payment(world: World) -> None:
    world.a.charge_results = [ok("a-1")]

    first, created1 = await world.engine.create_payment(card_cmd())
    second, created2 = await world.engine.create_payment(card_cmd())

    assert created1 and not created2
    assert first.id == second.id
    assert world.a.calls == ["charge"]


async def test_same_key_different_body_conflicts(world: World) -> None:
    world.a.charge_results = [ok("a-1")]
    await world.engine.create_payment(card_cmd())

    with pytest.raises(IdempotencyConflict):
        await world.engine.create_payment(card_cmd(amount="99.00"))


async def test_concurrent_requests_with_same_key_charge_once(world: World) -> None:
    world.a.charge_results = [ok("a-1")]

    results = await asyncio.gather(*(world.engine.create_payment(card_cmd()) for _ in range(5)))

    assert len({p.id for p, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert world.a.calls == ["charge"]


# ---------------------------------------------------------------- возврат


async def test_refund_payment(world: World) -> None:
    world.a.charge_results = [ok("a-1")]
    payment, _ = await world.engine.create_payment(card_cmd())

    payment = await world.engine.refund_payment(payment.id)

    assert payment.status is PaymentStatus.REFUNDED
    assert world.a.refunded == ["a-1"]


# ---------------------------------------------------------------- находки ревью


async def test_late_success_after_not_created_is_refunded(world: World) -> None:
    """Сочли попытку «не созданной», ушли к B, а A всё-таки провёл списание."""
    world.a.charge_results = [ProviderResult.unknown("timeout")]
    world.a.find_result = None
    world.b.charge_results = [ok("b-1")]
    payment, _ = await world.engine.create_payment(card_cmd())
    first = payment.attempts[0]
    assert first.status is AttemptStatus.ERROR and first.decline_code == "not_created"

    world.a.find_result = ok("a-late")
    await world.engine.sync_attempt(first.id)

    payment = await world.engine.get_payment(payment.id)
    assert payment.attempts[0].status is AttemptStatus.SUCCEEDED
    assert payment.attempts[0].is_duplicate
    assert payment.succeeded_attempt_id == payment.attempts[1].id
    assert world.queue.refunds == [first.id]


async def test_adapter_exception_becomes_unknown_not_stuck_pending(world: World) -> None:
    async def boom(req):  # type: ignore[no-untyped-def]
        raise KeyError("link_page_url")

    world.a.charge = boom  # type: ignore[method-assign]
    world.a.find_result = ProviderResult(AttemptStatus.UNKNOWN, None)

    payment, _ = await world.engine.create_payment(card_cmd())

    assert payment.attempts[0].status is AttemptStatus.UNKNOWN
    assert payment.status is PaymentStatus.PROCESSING
    assert world.b.calls == []


async def test_3ds_completion_timeout_is_polled(world: World) -> None:
    world.a.charge_results = [ProviderResult(AttemptStatus.REQUIRES_ACTION, "a-1",
                                             next_action=NextAction("3ds", "https://acs"))]
    world.a.complete_results = [ProviderResult.unknown("timeout")]
    payment, _ = await world.engine.create_payment(card_cmd())

    payment = await world.engine.complete_3ds(payment.attempts[0].id, {"PaRes": "ok"})

    assert payment.attempts[0].status is AttemptStatus.UNKNOWN
    assert payment.status is PaymentStatus.PROCESSING
    assert world.queue.polls == [payment.attempts[0].id]


async def test_pending_hosted_link_stays_requires_action(world: World) -> None:
    """Неоплаченный счёт PayMaster отдаётся как Pending → UNKNOWN; ссылка должна остаться."""
    world.pally.link_results = [ProviderResult.error("http_500", DeclineClass.SOFT)]
    world.a.link_results = [link("pm-1")]
    payment, _ = await world.engine.create_payment(link_cmd())
    world.a.status_result = ProviderResult(AttemptStatus.UNKNOWN, "pm-1", decline_code="pending")

    payment = await world.engine.sync_attempt(payment.attempts[-1].id)

    assert payment.status is PaymentStatus.REQUIRES_ACTION
    assert payment.attempts[-1].status is AttemptStatus.REQUIRES_ACTION


async def test_resume_continues_stalled_payment(world: World, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Процесс упал сразу после создания платежа: reconcile продолжает каскад по hosted."""
    async def crash(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(world.engine, "drive", crash)
    payment, _ = await world.engine.create_payment(card_cmd())
    assert payment.status is PaymentStatus.CREATED
    monkeypatch.undo()

    world.pally.link_results = [link("bill-1")]
    await world.engine.resume(payment.id)

    payment = await world.engine.get_payment(payment.id)
    assert payment.status is PaymentStatus.REQUIRES_ACTION
    assert statuses(payment) == [("pally", AttemptStatus.REQUIRES_ACTION)]
    assert world.a.calls == []  # карты нет — H2H пропущен


async def test_resume_does_not_start_attempt_while_one_is_in_flight(world: World) -> None:
    world.a.charge_results = [ProviderResult.unknown("timeout")]
    world.a.find_result = ProviderResult(AttemptStatus.UNKNOWN, None)
    payment, _ = await world.engine.create_payment(card_cmd())

    await world.engine.resume(payment.id)

    payment = await world.engine.get_payment(payment.id)
    assert len(payment.attempts) == 1
    assert world.pally.calls == []


async def test_redis_failure_does_not_block_payments(world: World) -> None:
    async def broken(provider: str) -> bool:
        raise ConnectionError("redis down")

    world.breaker.is_open = broken  # type: ignore[method-assign]
    world.a.charge_results = [ok("a-1")]

    payment, _ = await world.engine.create_payment(card_cmd())

    assert payment.status is PaymentStatus.SUCCEEDED

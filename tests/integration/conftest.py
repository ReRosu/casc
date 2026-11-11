"""Интеграционные тесты движка: настоящий Postgres (БД casc_test), фейковые провайдеры.

Postgres берётся из docker compose (`docker compose up -d postgres`), URL можно
переопределить через TEST_DATABASE_URL.
"""

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.cascade.engine import CascadeEngine
from app.core.config import Settings
from app.db.models import Base, RoutingRule
from app.domain.enums import AttemptStatus, Flow, ProviderMode
from app.domain.results import ProviderResult
from app.providers.base import ProviderRequest, RefundResult
from app.providers.registry import ProviderRegistry
from app.routing.circuit_breaker import InMemoryCircuitBreaker

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://casc:casc@localhost:55432/casc_test")


async def _ensure_database(url: str) -> None:
    dsn = url.replace("+asyncpg", "")
    base, db_name = dsn.rsplit("/", 1)
    conn = await asyncpg.connect(f"{base}/postgres")
    try:
        if not await conn.fetchval("select 1 from pg_database where datname = $1", db_name):
            await conn.execute(f'create database "{db_name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def sm() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    await _ensure_database(TEST_DB_URL)
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s, s.begin():
        rub = ["RUB"]
        s.add_all([
            RoutingRule(provider="paymaster_a", flow=Flow.CARD, mode=ProviderMode.H2H,
                        priority=10, currencies=rub, methods=["bankcard"],
                        fee_percent=Decimal("2.5")),
            RoutingRule(provider="paymaster_b", flow=Flow.CARD, mode=ProviderMode.H2H,
                        priority=20, currencies=rub, methods=["bankcard"],
                        fee_percent=Decimal("2.8")),
            RoutingRule(provider="pally", flow=Flow.CARD, mode=ProviderMode.HOSTED,
                        priority=30, currencies=rub, methods=["bankcard"],
                        fee_percent=Decimal("3")),
            RoutingRule(provider="pally", flow=Flow.LINK, mode=ProviderMode.HOSTED,
                        priority=10, currencies=rub, methods=["bankcard", "sbp"],
                        fee_percent=Decimal("1.5")),
            RoutingRule(provider="paymaster_a", flow=Flow.LINK, mode=ProviderMode.HOSTED,
                        priority=20, currencies=rub, methods=["bankcard", "sbp"],
                        fee_percent=Decimal("2")),
        ])
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(sm: async_sessionmaker[AsyncSession]) -> None:
    async with sm() as s, s.begin():
        await s.execute(text("update payments set succeeded_attempt_id = null"))
        await s.execute(text("truncate payment_attempts, payments, webhook_inbox cascade"))


@dataclass
class FakeAdapter:
    """Провайдер, ответами которого управляет тест."""

    name: str
    modes: frozenset[ProviderMode]
    charge_results: list[ProviderResult] = field(default_factory=list)
    link_results: list[ProviderResult] = field(default_factory=list)
    complete_results: list[ProviderResult] = field(default_factory=list)
    status_result: ProviderResult | None = None
    find_result: ProviderResult | None = None
    calls: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    refunded: list[str] = field(default_factory=list)

    async def charge(self, req: ProviderRequest) -> ProviderResult:
        assert req.card is not None
        self.calls.append("charge")
        return self.charge_results.pop(0)

    async def create_link(self, req: ProviderRequest) -> ProviderResult:
        assert req.card is None, "card data must never reach hosted providers"
        self.calls.append("create_link")
        return self.link_results.pop(0)

    async def complete_3ds(self, provider_payment_id: str, params: dict[str, str]) -> ProviderResult:
        self.calls.append("complete_3ds")
        return self.complete_results.pop(0)

    async def get_status(self, provider_payment_id: str) -> ProviderResult:
        self.calls.append("get_status")
        assert self.status_result is not None
        return self.status_result

    async def find_by_order(self, attempt_id: str, created_after: datetime) -> ProviderResult | None:
        self.calls.append("find_by_order")
        return self.find_result

    async def cancel_link(self, provider_payment_id: str) -> bool:
        self.cancelled.append(provider_payment_id)
        return True

    async def refund(self, provider_payment_id: str, amount: Decimal, currency: str,
                     idempotency_key: str) -> RefundResult:
        self.refunded.append(provider_payment_id)
        return RefundResult(ok=True, refund_id="r1", status="Success")

    async def aclose(self) -> None:
        pass


class FakeQueue:
    """Вместо taskiq: копит поставленные задачи, тест сам решает, когда их выполнить."""

    def __init__(self) -> None:
        self.polls: list[uuid.UUID] = []
        self.orphans: list[uuid.UUID] = []
        self.refunds: list[uuid.UUID] = []

    async def poll_unknown(self, attempt_id: uuid.UUID) -> None:
        self.polls.append(attempt_id)

    async def cleanup_orphan(self, attempt_id: uuid.UUID) -> None:
        self.orphans.append(attempt_id)

    async def refund_duplicate(self, attempt_id: uuid.UUID) -> None:
        self.refunds.append(attempt_id)


@dataclass
class World:
    engine: CascadeEngine
    a: FakeAdapter
    b: FakeAdapter
    pally: FakeAdapter
    queue: FakeQueue
    breaker: InMemoryCircuitBreaker
    settings: Settings


def make_world(sm: async_sessionmaker[AsyncSession], **overrides: object) -> World:
    settings = Settings(
        unknown_inline_polls=1, unknown_inline_poll_interval=0,
        unknown_not_found_grace_seconds=0, **overrides,  # type: ignore[arg-type]
    )
    both = frozenset({ProviderMode.H2H, ProviderMode.HOSTED})
    a = FakeAdapter("paymaster_a", both)
    b = FakeAdapter("paymaster_b", both)
    pally = FakeAdapter("pally", frozenset({ProviderMode.HOSTED}))
    queue = FakeQueue()
    breaker = InMemoryCircuitBreaker(threshold=3, open_seconds=30)
    registry = ProviderRegistry({"paymaster_a": a, "paymaster_b": b, "pally": pally})
    engine = CascadeEngine(sm, registry, breaker, queue, settings)  # type: ignore[arg-type]
    return World(engine, a, b, pally, queue, breaker, settings)


@pytest.fixture
def world(sm: async_sessionmaker[AsyncSession]) -> World:
    return make_world(sm)


def ok(ppid: str) -> ProviderResult:
    return ProviderResult(AttemptStatus.SUCCEEDED, ppid)

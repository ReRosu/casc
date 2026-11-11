"""InMemoryCircuitBreaker: порог срабатывания, сброс, независимость провайдеров."""

import pytest

from app.routing.circuit_breaker import InMemoryCircuitBreaker


def make_cb() -> InMemoryCircuitBreaker:
    return InMemoryCircuitBreaker(threshold=3, open_seconds=30)


async def test_below_threshold_stays_closed() -> None:
    cb = make_cb()

    await cb.record_failure("p")
    await cb.record_failure("p")

    assert await cb.is_open("p") is False


async def test_third_failure_opens() -> None:
    cb = make_cb()

    await cb.record_failure("p")
    await cb.record_failure("p")
    await cb.record_failure("p")

    assert await cb.is_open("p") is True


async def test_record_success_resets_failure_counter() -> None:
    cb = make_cb()

    await cb.record_failure("p")
    await cb.record_failure("p")
    await cb.record_success("p")
    await cb.record_failure("p")
    await cb.record_failure("p")

    assert await cb.is_open("p") is False


async def test_reset_closes_open_breaker() -> None:
    cb = make_cb()

    await cb.record_failure("p")
    await cb.record_failure("p")
    await cb.record_failure("p")
    assert await cb.is_open("p") is True

    await cb.reset("p")

    assert await cb.is_open("p") is False


async def test_force_open_with_zero_seconds_is_not_open() -> None:
    cb = make_cb()

    await cb.force_open("p", 0)

    assert await cb.is_open("p") is False


async def test_force_open_with_positive_seconds_opens() -> None:
    cb = make_cb()

    await cb.force_open("p", 30)

    assert await cb.is_open("p") is True


async def test_providers_are_independent() -> None:
    cb = make_cb()

    await cb.record_failure("a")
    await cb.record_failure("a")
    await cb.record_failure("a")

    assert await cb.is_open("a") is True
    assert await cb.is_open("b") is False


@pytest.mark.parametrize("provider", ["", "paymaster_a", "pally"])
async def test_new_breaker_is_closed_for_any_provider(provider: str) -> None:
    assert await make_cb().is_open(provider) is False

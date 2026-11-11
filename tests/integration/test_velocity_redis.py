"""RedisVelocityLimiter на настоящем Redis (docker compose up -d redis)."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.routing.velocity import RedisVelocityLimiter

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/1")


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    yield client
    await client.aclose()


async def test_limit_and_rejected_hits_do_not_consume_quota(redis: Redis) -> None:
    fp = uuid.uuid4().hex
    limiter = RedisVelocityLimiter(redis, limit=2, window_seconds=60)

    assert [await limiter.hit(fp) for _ in range(5)] == [True, True, False, False, False]
    assert await redis.get(f"vel:{fp}") == "2"  # отказы счётчик не раздувают
    assert 0 < await redis.ttl(f"vel:{fp}") <= 60

    await limiter.release(fp)
    assert await limiter.hit(fp) is True  # освободившееся место снова доступно


async def test_cards_are_independent(redis: Redis) -> None:
    limiter = RedisVelocityLimiter(redis, limit=1, window_seconds=60)

    assert await limiter.hit(uuid.uuid4().hex)
    assert await limiter.hit(uuid.uuid4().hex)


async def test_redis_down_allows_payment() -> None:
    dead = Redis.from_url("redis://localhost:1/0", socket_connect_timeout=0.2)
    limiter = RedisVelocityLimiter(dead, limit=1, window_seconds=60)

    assert await limiter.hit("any") is True
    await dead.aclose()

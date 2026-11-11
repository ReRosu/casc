"""Circuit breaker на провайдера: N технических сбоев за окно → провайдер выключается
из маршрутов на cb_open_seconds. Бизнес-отказы (нет денег и т.п.) не считаются —
провайдер жив, проблема в карте.

Состояние в Redis, чтобы все инстансы API/воркеров видели одно и то же.
"""

import time
from typing import Protocol

from redis.asyncio import Redis


class CircuitBreaker(Protocol):
    async def is_open(self, provider: str) -> bool: ...
    async def record_failure(self, provider: str) -> None: ...
    async def record_success(self, provider: str) -> None: ...
    async def force_open(self, provider: str, seconds: int) -> None: ...
    async def reset(self, provider: str) -> None: ...


class RedisCircuitBreaker:
    def __init__(self, redis: Redis, *, threshold: int, window_seconds: int,
                 open_seconds: int) -> None:
        self._redis = redis
        self._threshold = threshold
        self._window = window_seconds
        self._open = open_seconds

    async def is_open(self, provider: str) -> bool:
        return bool(await self._redis.exists(f"cb:{provider}:open"))

    async def record_failure(self, provider: str) -> None:
        key = f"cb:{provider}:fails"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, self._window, nx=True)
            fails, _ = await pipe.execute()
        if fails >= self._threshold:
            await self.force_open(provider, self._open)

    async def record_success(self, provider: str) -> None:
        await self._redis.delete(f"cb:{provider}:fails")

    async def force_open(self, provider: str, seconds: int) -> None:
        await self._redis.set(f"cb:{provider}:open", "1", ex=seconds)
        await self._redis.delete(f"cb:{provider}:fails")

    async def reset(self, provider: str) -> None:
        await self._redis.delete(f"cb:{provider}:open", f"cb:{provider}:fails")


class InMemoryCircuitBreaker:
    """Для тестов и локального запуска без Redis."""

    def __init__(self, *, threshold: int = 3, open_seconds: int = 30) -> None:
        self._threshold = threshold
        self._open_seconds = open_seconds
        self._fails: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    async def is_open(self, provider: str) -> bool:
        return self._open_until.get(provider, 0) > time.monotonic()

    async def record_failure(self, provider: str) -> None:
        self._fails[provider] = self._fails.get(provider, 0) + 1
        if self._fails[provider] >= self._threshold:
            await self.force_open(provider, self._open_seconds)

    async def record_success(self, provider: str) -> None:
        self._fails.pop(provider, None)

    async def force_open(self, provider: str, seconds: int) -> None:
        self._open_until[provider] = time.monotonic() + seconds
        self._fails.pop(provider, None)

    async def reset(self, provider: str) -> None:
        self._open_until.pop(provider, None)
        self._fails.pop(provider, None)

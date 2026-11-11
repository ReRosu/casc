"""Лимит платежей по одной карте за окно: каскад не должен стать инструментом перебора карт.

Счётчик в Redis: атомарный INCR + TTL, без нагрузки на основные таблицы и без гонок
«проверил — вставил». Окно фиксированное: на стыке окон возможен всплеск до 2×лимита.
Redis недоступен — пропускаем платёж (лучше принять платёж, чем встать из-за счётчика).
"""

import logging
import time
from typing import Protocol

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class VelocityLimiter(Protocol):
    async def hit(self, fingerprint: str) -> bool:
        """Учесть новый платёж по карте. False — лимит превышен (платёж не учтён)."""
        ...

    async def release(self, fingerprint: str) -> None:
        """Откатить учтённый платёж (вставка в БД не удалась)."""
        ...


class RedisVelocityLimiter:
    def __init__(self, redis: Redis, *, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._limit = limit
        self._window = window_seconds

    @staticmethod
    def _key(fingerprint: str) -> str:
        return f"vel:{fingerprint}"

    async def hit(self, fingerprint: str) -> bool:
        key = self._key(fingerprint)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._window, nx=True)
                count, _ = await pipe.execute()
            if count > self._limit:
                await self._redis.decr(key)  # отклонённый платёж не расходует лимит
                return False
        except Exception:
            logger.exception("velocity limiter unavailable — allowing payment")
        return True

    async def release(self, fingerprint: str) -> None:
        try:
            await self._redis.decr(self._key(fingerprint))
        except Exception:
            logger.exception("velocity release failed")


class InMemoryVelocityLimiter:
    """Для тестов."""

    def __init__(self, *, limit: int) -> None:
        self._limit = limit
        self._counts: dict[str, int] = {}
        self._started = time.monotonic()

    async def hit(self, fingerprint: str) -> bool:
        if self._counts.get(fingerprint, 0) >= self._limit:
            return False
        self._counts[fingerprint] = self._counts.get(fingerprint, 0) + 1
        return True

    async def release(self, fingerprint: str) -> None:
        self._counts[fingerprint] = max(0, self._counts.get(fingerprint, 0) - 1)

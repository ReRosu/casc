from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import get_settings


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def close_redis() -> None:
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
    get_redis.cache_clear()

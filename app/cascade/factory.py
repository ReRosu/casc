from functools import lru_cache

from app.cascade.engine import CascadeEngine
from app.core.config import get_settings
from app.core.redis import get_redis
from app.db.db import get_sessionmaker
from app.providers.registry import get_registry
from app.routing.circuit_breaker import RedisCircuitBreaker
from app.worker.queue import TaskiqQueue


@lru_cache
def get_breaker() -> RedisCircuitBreaker:
    settings = get_settings()
    return RedisCircuitBreaker(
        get_redis(),
        threshold=settings.cb_failure_threshold,
        window_seconds=settings.cb_window_seconds,
        open_seconds=settings.cb_open_seconds,
    )


@lru_cache
def get_cascade() -> CascadeEngine:
    return CascadeEngine(get_sessionmaker(), get_registry(), get_breaker(), TaskiqQueue(),
                         get_settings())

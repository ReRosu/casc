"""taskiq CLI требует брокер на уровне модуля (`taskiq worker app.worker.broker:broker`),
поэтому здесь — единственное место, где настройки читаются при импорте."""

import logging

from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker

from app.core.config import get_settings

# BRPOP блокируется без таймаута, а redis-py 8 по умолчанию рвёт чтение через 5 с
# (socket_timeout=5) — воркер падал бы каждые 5 секунд. Мёртвое соединение ловит keepalive.
broker = ListQueueBroker(
    url=get_settings().redis_url,
    queue_name="casc",
    socket_timeout=None,
    socket_keepalive=True,
)
scheduler = TaskiqScheduler(broker, sources=[LabelScheduleSource(broker)])


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _startup(_: TaskiqState) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _shutdown(_: TaskiqState) -> None:
    from app.core.redis import close_redis
    from app.db.db import dispose_engine
    from app.providers.registry import close_registry

    await close_registry()
    await close_redis()
    await dispose_engine()


# Регистрируем задачи у брокера.
import app.worker.tasks  # noqa: E402, F401

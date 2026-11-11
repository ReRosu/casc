import uuid


class TaskiqQueue:
    """Реализация TaskQueue движка поверх taskiq. Импорт задач ленивый — разрывает цикл
    engine → queue → tasks → engine."""

    async def poll_unknown(self, attempt_id: uuid.UUID) -> None:
        from app.worker.tasks import poll_unknown

        await poll_unknown.kiq(str(attempt_id))

    async def cleanup_orphan(self, attempt_id: uuid.UUID) -> None:
        from app.worker.tasks import cleanup_orphan

        await cleanup_orphan.kiq(str(attempt_id))

    async def refund_duplicate(self, attempt_id: uuid.UUID) -> None:
        from app.worker.tasks import refund_duplicate

        await refund_duplicate.kiq(str(attempt_id))

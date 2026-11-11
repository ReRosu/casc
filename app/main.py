import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import payments, providers, returns, webhooks
from app.core.redis import close_redis
from app.db.db import dispose_engine
from app.providers.registry import close_registry
from app.worker.broker import broker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await broker.startup()
    try:
        yield
    finally:
        await broker.shutdown()
        await close_registry()
        await close_redis()
        await dispose_engine()


app = FastAPI(title="Payment cascading", version="0.1.0", lifespan=lifespan)
app.include_router(payments.router)
app.include_router(returns.router)
app.include_router(webhooks.router)
app.include_router(providers.router)


@app.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}

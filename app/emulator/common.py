import asyncio
import logging
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request

from app.emulator.config import get_emulator_settings

logger = logging.getLogger("emulator")

_background: set[asyncio.Task[None]] = set()


class Outage:
    """Имитация падения провайдера: все API-запросы отвечают 503."""

    def __init__(self) -> None:
        self._until = 0.0

    def set(self, seconds: float) -> None:
        self._until = time.monotonic() + seconds

    def clear(self) -> None:
        self._until = 0.0

    @property
    def active(self) -> bool:
        return time.monotonic() < self._until


def check_bearer(request: Request, token: str) -> None:
    if request.headers.get("Authorization") != f"Bearer {token}":
        raise HTTPException(401, detail={"code": "not_authorized", "message": "Unauthenticated."})


def send_callback(url: str, *, json: dict[str, Any] | None = None,
                  data: dict[str, str] | None = None) -> None:
    """Колбэк в фоне с ретраями, как у настоящих провайдеров (Pally: 10^n секунд, тут быстрее)."""
    target = get_emulator_settings().rewrite(url)

    async def _send() -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            for attempt in range(5):
                await asyncio.sleep(0.3 * 2**attempt)
                try:
                    resp = await client.post(target, json=json, data=data)
                    logger.info("callback %s → %s", target, resp.status_code)
                    if resp.status_code == 200:
                        return
                except httpx.HTTPError as exc:
                    logger.warning("callback %s failed: %r", target, exc)

    task = asyncio.create_task(_send())
    _background.add(task)
    task.add_done_callback(_background.discard)


def page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:system-ui;max-width:480px;margin:48px auto}}
button{{font-size:16px;padding:10px 18px;margin-right:8px;cursor:pointer}}</style></head>
<body><h2>{title}</h2>{body}</body></html>"""

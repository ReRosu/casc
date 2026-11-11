import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from app.cascade.inbox import store_event
from app.core.security import verify_callback_signature
from app.db.db import get_sessionmaker
from app.providers.pally import PallyAdapter
from app.providers.registry import ProviderRegistry, get_registry
from app.worker.tasks import process_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

PAYMASTER_INSTANCES = frozenset({"paymaster_a", "paymaster_b"})


@router.post("/pally", response_class=PlainTextResponse)
async def pally_postback(
    request: Request, registry: Annotated[ProviderRegistry, Depends(get_registry)]
) -> str:
    form = {k: str(v) for k, v in (await request.form()).items()}
    adapter = registry.get("pally")
    assert isinstance(adapter, PallyAdapter)
    if not adapter.verify_postback(form):
        logger.warning("pally postback with bad signature: InvId=%s", form.get("InvId"))
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad signature")
    # Подпись Pally не покрывает Status, поэтому статус потом сверяем через API.
    payload = {k: v for k, v in form.items() if k != "SignatureValue"}
    dedup_key = f"{form.get('InvId')}:{form.get('TrsId')}:{form.get('Status')}"
    await _store_and_enqueue("pally", dedup_key, payload, signature_ok=True)
    return "OK"


@router.post("/paymaster/{instance}/{attempt_id}/{signature}",
             response_class=PlainTextResponse)
async def paymaster_notification(instance: str, attempt_id: uuid.UUID, signature: str,
                                 payload: Annotated[dict[str, Any], Body()]) -> str:
    # PayMaster не подписывает уведомления — подпись мы заранее положили в callbackUrl.
    if instance not in PAYMASTER_INSTANCES or not verify_callback_signature(
        instance, str(attempt_id), signature
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad signature")
    payload = payload | {"_attempt_id": str(attempt_id)}
    dedup_key = f"{attempt_id}:{payload.get('id')}:{payload.get('status')}"
    await _store_and_enqueue(instance, dedup_key, payload, signature_ok=True)
    return "OK"


async def _store_and_enqueue(provider: str, dedup_key: str, payload: dict[str, Any],
                             signature_ok: bool | None) -> None:
    event_id = await store_event(get_sessionmaker(), provider, dedup_key, payload, signature_ok)
    if event_id is None:
        logger.info("duplicate webhook %s %s — ignored", provider, dedup_key)
        return
    try:
        await process_webhook.kiq(str(event_id))
    except Exception:  # событие уже в inbox — reconcile обработает
        logger.exception("failed to enqueue webhook %s", event_id)

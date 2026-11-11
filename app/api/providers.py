from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import ProviderOut
from app.cascade.factory import get_breaker
from app.db.db import get_session
from app.db.models import RoutingRule
from app.providers.registry import ProviderRegistry, get_registry
from app.routing.circuit_breaker import CircuitBreaker

router = APIRouter(prefix="/v1/providers", tags=["providers"])


@router.get("", response_model=list[ProviderOut])
async def list_providers(session: Annotated[AsyncSession, Depends(get_session)]) -> list[ProviderOut]:
    rules = (await session.scalars(
        select(RoutingRule).order_by(RoutingRule.flow, RoutingRule.priority))).all()
    breaker = get_breaker()
    return [
        ProviderOut(provider=r.provider, flow=r.flow, mode=r.mode, priority=r.priority,
                    enabled=r.enabled, fee_percent=r.fee_percent,
                    circuit_open=await breaker.is_open(r.provider))
        for r in rules
    ]


@router.post("/{provider}/circuit/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_circuit(
    provider: str,
    registry: Annotated[ProviderRegistry, Depends(get_registry)],
    breaker: Annotated[CircuitBreaker, Depends(get_breaker)],
) -> None:
    """Ручное закрытие circuit breaker после инцидента у провайдера."""
    if provider not in registry.names():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown provider")
    await breaker.reset(provider)

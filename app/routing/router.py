from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RoutingRule
from app.domain.enums import Flow, PaymentMethod, ProviderMode


@dataclass(frozen=True, slots=True)
class RouteStep:
    provider: str
    mode: ProviderMode
    priority: int
    fee_percent: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"mode": self.mode.value}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RouteStep:
        return cls(d["provider"], ProviderMode(d["mode"]), d["priority"], d["fee_percent"])


def build_route(rules: list[RoutingRule], *, flow: Flow, amount: Decimal, currency: str,
                method: PaymentMethod) -> list[RouteStep]:
    """Подходящие провайдеры в порядке попыток.

    Сортировка: H2H раньше hosted (H2H каскадит незаметно для клиента, а после
    переадресации на hosted-страницу автоматический каскад заканчивается),
    дальше priority, при равенстве — дешевле.
    """
    eligible = [
        r for r in rules
        if r.enabled
        and r.flow == flow
        and currency in r.currencies
        and method.value in r.methods
        and (r.min_amount is None or amount >= r.min_amount)
        and (r.max_amount is None or amount <= r.max_amount)
    ]
    eligible.sort(key=lambda r: (r.mode != ProviderMode.H2H, r.priority, r.fee_percent))
    return [RouteStep(r.provider, r.mode, r.priority, str(r.fee_percent)) for r in eligible]


async def load_rules(session: AsyncSession) -> list[RoutingRule]:
    return list((await session.scalars(select(RoutingRule))).all())

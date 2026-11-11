from decimal import Decimal

from sqlalchemy import Boolean, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base, str_enum
from app.domain.enums import Flow, ProviderMode


class RoutingRule(Base):
    """Одна строка = «провайдер X участвует в каскаде режима Y с таким приоритетом»."""

    __tablename__ = "routing_rules"
    __table_args__ = (UniqueConstraint("provider", "flow"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32))
    flow: Mapped[Flow] = mapped_column(str_enum(Flow))
    mode: Mapped[ProviderMode] = mapped_column(str_enum(ProviderMode))
    priority: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    currencies: Mapped[list[str]] = mapped_column(ARRAY(String(3)))
    methods: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    min_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    max_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    fee_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))

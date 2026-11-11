from dataclasses import dataclass, field
from typing import Any

from app.domain.enums import AttemptStatus, DeclineClass


@dataclass(frozen=True, slots=True)
class NextAction:
    type: str  # "redirect" | "3ds"
    url: str
    method: str = "GET"
    params: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "url": self.url, "method": self.method, "params": self.params}


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Нормализованный ответ провайдера. Адаптер переводит сюда всё, что вернул API."""

    status: AttemptStatus
    provider_payment_id: str | None = None
    next_action: NextAction | None = None
    decline_code: str | None = None
    decline_class: DeclineClass | None = None
    message: str | None = None
    # Технический сбой провайдера (5xx, сеть, таймаут) — учитывается circuit breaker'ом.
    technical_failure: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def unknown(cls, code: str, message: str | None = None) -> ProviderResult:
        return cls(
            status=AttemptStatus.UNKNOWN,
            decline_code=code,
            decline_class=DeclineClass.UNKNOWN,
            message=message,
            technical_failure=True,
        )

    @classmethod
    def error(cls, code: str, decline_class: DeclineClass, message: str | None = None,
              *, technical: bool = False, raw: dict[str, Any] | None = None) -> ProviderResult:
        return cls(
            status=AttemptStatus.ERROR,
            decline_code=code,
            decline_class=decline_class,
            message=message,
            technical_failure=technical,
            raw=raw or {},
        )


@dataclass(frozen=True, slots=True)
class CardData:
    """Данные карты живут только в памяти в рамках одного запроса. Никогда не сохраняются."""

    pan: str
    expiry: str
    cvc: str

    def __repr__(self) -> str:  # не даём PAN/CVC утечь в логи через repr
        return f"CardData(pan=****{self.pan[-4:]})"

    @property
    def masked_pan(self) -> str:
        return f"{self.pan[:6]}******{self.pan[-4:]}"

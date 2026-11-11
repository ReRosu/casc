import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx

from app.domain.enums import DeclineClass, PaymentMethod, ProviderMode, Stage
from app.domain.results import CardData, ProviderResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    attempt_id: str  # уходит провайдеру как order_id / orderNo
    idempotency_key: str
    amount: Decimal
    currency: str
    description: str
    method: PaymentMethod
    customer_email: str | None
    return_url: str  # куда провайдер вернёт браузер клиента (наш /v1/return/{attempt_id})
    callback_url: str  # куда провайдер шлёт вебхук
    link_ttl_seconds: int
    card: CardData | None = None


@dataclass(frozen=True, slots=True)
class RefundResult:
    ok: bool
    refund_id: str | None = None
    status: str | None = None
    message: str | None = None


class ProviderAdapter(Protocol):
    name: str
    modes: frozenset[ProviderMode]

    async def charge(self, req: ProviderRequest) -> ProviderResult:
        """H2H: провести оплату картой."""
        ...

    async def create_link(self, req: ProviderRequest) -> ProviderResult:
        """Hosted: получить ссылку/реквизиты, куда отправить клиента."""
        ...

    async def complete_3ds(self, provider_payment_id: str, params: dict[str, str]) -> ProviderResult:
        ...

    async def get_status(self, provider_payment_id: str) -> ProviderResult:
        ...

    async def find_by_order(self, attempt_id: str, created_after: datetime) -> ProviderResult | None:
        """Найти операцию по нашему id попытки (когда provider_payment_id неизвестен — таймаут).
        None — провайдер ничего не создал."""
        ...

    async def cancel_link(self, provider_payment_id: str) -> bool:
        ...

    async def refund(self, provider_payment_id: str, amount: Decimal, currency: str,
                     idempotency_key: str) -> RefundResult:
        ...

    async def aclose(self) -> None:
        ...


class TransportFailure(Exception):
    def __init__(self, result: ProviderResult) -> None:
        super().__init__(result.decline_code)
        self.result = result


class HttpAdapter:
    """Общая часть: перевод сетевых ошибок в ProviderResult.

    Ключевое различие для защиты от двойного списания:
    * запрос точно НЕ дошёл (connect error) → ERROR/SOFT, можно каскадить;
    * запрос ушёл, ответа нет (read timeout, обрыв) → UNKNOWN, сначала выяснить статус.
    """

    name: str

    def __init__(self, name: str, base_url: str, headers: dict[str, str],
                 connect_timeout: float, read_timeout: float,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = name
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(self, stage: Stage, method: str, url: str, **kwargs: Any) -> httpx.Response:
        started = time.perf_counter()
        try:
            response = await self._client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise TransportFailure(ProviderResult.error(
                "connect_error", DeclineClass.SOFT, repr(exc), technical=True,
            )) from exc
        except httpx.TimeoutException as exc:
            raise TransportFailure(ProviderResult.unknown("timeout", repr(exc))) from exc
        except httpx.HTTPError as exc:  # обрыв, ошибка записи/декодирования — запрос мог дойти
            raise TransportFailure(ProviderResult.unknown("network_error", repr(exc))) from exc
        finally:
            logger.info("%s %s %s took %.0fms", self.name, method, url,
                        (time.perf_counter() - started) * 1000)

        if response.status_code >= 500:
            # На списании 500/502/504 не доказывают, что денег не взяли (ошибка после проведения,
            # таймаут прокси). Выясняем статус — пара секунд вместо двойного списания.
            # 503 по семантике HTTP — «запрос не принят в обработку», его можно каскадить.
            if stage is Stage.CHARGE and response.status_code != 503:
                raise TransportFailure(ProviderResult.unknown(f"http_{response.status_code}"))
            raise TransportFailure(ProviderResult.error(
                f"http_{response.status_code}", DeclineClass.SOFT,
                response.text[:200], technical=True,
            ))
        return response


def safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {"_text": response.text[:500]}
    return data if isinstance(data, dict) else {"_data": data}

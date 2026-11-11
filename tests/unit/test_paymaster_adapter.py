"""PayMasterAdapter: списание, 3DS, ошибки, ссылка, поиск по orderNo, маскирование карты."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from app.core.config import PayMasterSettings
from app.domain.enums import AttemptStatus, DeclineClass, PaymentMethod
from app.domain.results import CardData
from app.providers.base import ProviderRequest
from app.providers.paymaster import PayMasterAdapter

BASE_URL = "https://pm.test"
CARD = CardData("4111111111111111", "12/29", "123")


@pytest.fixture
async def adapter() -> AsyncIterator[PayMasterAdapter]:
    a = PayMasterAdapter(
        "paymaster_a",
        PayMasterSettings(base_url=BASE_URL, token="tok", merchant_id="M"),
        connect_timeout=1,
        read_timeout=1,
    )
    try:
        yield a
    finally:
        await a.aclose()


def make_request(**overrides: Any) -> ProviderRequest:
    values: dict[str, Any] = {
        "attempt_id": "att-1",
        "idempotency_key": "k",
        "amount": Decimal("100.00"),
        "currency": "RUB",
        "description": "d",
        "method": PaymentMethod.BANKCARD,
        "customer_email": None,
        "return_url": "https://shop/return",
        "callback_url": "https://shop/cb",
        "link_ttl_seconds": 600,
        "card": CARD,
    }
    values.update(overrides)
    return ProviderRequest(**values)


async def charge(adapter: PayMasterAdapter, **overrides: Any) -> tuple[Any, Any]:
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/v2/payments").mock(
            return_value=httpx.Response(**overrides)
        )
        result = await adapter.charge(make_request())
    return result, route


# --- charge ---


async def test_charge_settled_is_succeeded(adapter: PayMasterAdapter) -> None:
    result, route = await charge(adapter, status_code=200, json={"id": "1", "status": "Settled"})

    assert result.status is AttemptStatus.SUCCEEDED
    assert result.provider_payment_id == "1"

    request = route.calls.last.request
    assert request.headers["Idempotency-Key"] == "k"
    body = json.loads(request.content)
    assert body["invoice"]["orderNo"] == "att-1"


@pytest.mark.parametrize(
    ("result_code", "expected_class"),
    [("InsufficientFunds", DeclineClass.HARD), ("TransactionDeclined", DeclineClass.SOFT)],
)
async def test_charge_rejected_uses_result_code_class(
    adapter: PayMasterAdapter, result_code: str, expected_class: DeclineClass
) -> None:
    result, _ = await charge(
        adapter, status_code=200, json={"id": "2", "status": "Rejected", "resultCode": result_code}
    )

    assert result.status is AttemptStatus.DECLINED
    assert result.decline_code == result_code
    assert result.decline_class is expected_class


async def test_charge_confirmation_maps_to_3ds(adapter: PayMasterAdapter) -> None:
    payload = {
        "id": "3",
        "status": "Confirmation",
        "confirmation": {"type": "ThreeDSv1", "acsUrl": "https://acs", "PAReq": "pa"},
    }
    result, _ = await charge(adapter, status_code=200, json=payload)

    assert result.status is AttemptStatus.REQUIRES_ACTION
    assert result.next_action is not None
    assert result.next_action.type == "3ds"
    assert result.next_action.params["PaReq"] == "pa"
    assert result.next_action.params["TermUrl"] == "https://shop/return"


async def test_charge_pending_is_unknown(adapter: PayMasterAdapter) -> None:
    result, _ = await charge(adapter, status_code=200, json={"id": "4", "status": "Pending"})

    assert result.status is AttemptStatus.UNKNOWN


async def test_charge_http_502_is_unknown(adapter: PayMasterAdapter) -> None:
    result, _ = await charge(adapter, status_code=502, json={})

    assert result.status is AttemptStatus.UNKNOWN


async def test_charge_http_500_is_unknown(adapter: PayMasterAdapter) -> None:
    """500 на списании не доказывает, что денег не взяли — исход выясняем."""
    result, _ = await charge(adapter, status_code=500, json={})

    assert result.status is AttemptStatus.UNKNOWN
    assert result.technical_failure is True


async def test_charge_http_503_is_soft_error(adapter: PayMasterAdapter) -> None:
    """503 — запрос не принят в обработку: можно идти к следующему шлюзу."""
    result, _ = await charge(adapter, status_code=503, json={})

    assert result.status is AttemptStatus.ERROR
    assert result.decline_class is DeclineClass.SOFT
    assert result.technical_failure is True


async def test_invoice_http_500_is_soft_error(adapter: PayMasterAdapter) -> None:
    """На этапе ссылки денег нет — 500 означает просто «иди к следующему»."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/v2/invoices").mock(return_value=httpx.Response(500, json={}))
        result = await adapter.create_link(make_request(card=None))

    assert result.status is AttemptStatus.ERROR
    assert result.decline_class is DeclineClass.SOFT
    assert result.technical_failure is True


async def test_charge_write_error_is_unknown(adapter: PayMasterAdapter) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/v2/payments").mock(side_effect=httpx.WriteError("broken pipe"))
        result = await adapter.charge(make_request())

    assert result.status is AttemptStatus.UNKNOWN


async def test_charge_read_timeout_is_unknown(adapter: PayMasterAdapter) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/v2/payments").mock(side_effect=httpx.ReadTimeout("slow"))
        result = await adapter.charge(make_request())

    assert result.status is AttemptStatus.UNKNOWN


async def test_charge_connect_error_is_soft_error(adapter: PayMasterAdapter) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/v2/payments").mock(side_effect=httpx.ConnectError("no route"))
        result = await adapter.charge(make_request())

    assert result.status is AttemptStatus.ERROR
    assert result.decline_class is DeclineClass.SOFT
    assert result.technical_failure is True


async def test_charge_400_validation_error_is_hard(adapter: PayMasterAdapter) -> None:
    result, _ = await charge(
        adapter, status_code=400, json={"code": "validation_error", "message": "x"}
    )

    assert result.status is AttemptStatus.ERROR
    assert result.decline_class is DeclineClass.HARD


async def test_charge_401_without_body_is_config_error(adapter: PayMasterAdapter) -> None:
    result, _ = await charge(adapter, status_code=401)

    assert result.status is AttemptStatus.ERROR
    assert result.decline_class is DeclineClass.CONFIG


# --- create_link ---


async def test_create_link_returns_redirect(adapter: PayMasterAdapter) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/v2/invoices").mock(
            return_value=httpx.Response(200, json={"paymentId": "9", "url": "https://pm/pay/9"})
        )
        result = await adapter.create_link(make_request(card=None))

    assert result.status is AttemptStatus.REQUIRES_ACTION
    assert result.provider_payment_id == "9"
    assert result.next_action is not None
    assert result.next_action.type == "redirect"
    assert result.next_action.url == "https://pm/pay/9"


# --- find_by_order ---


async def test_find_by_order_returns_matching_payment(adapter: PayMasterAdapter) -> None:
    payload = {"items": [{"id": "5", "status": "Settled", "invoice": {"orderNo": "att-1"}}]}
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v2/payments").mock(return_value=httpx.Response(200, json=payload))
        result = await adapter.find_by_order("att-1", datetime.now(UTC))

    assert result is not None
    assert result.status is AttemptStatus.SUCCEEDED
    assert result.provider_payment_id == "5"


async def test_find_by_order_returns_none_when_no_items(adapter: PayMasterAdapter) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v2/payments").mock(return_value=httpx.Response(200, json={"items": []}))
        result = await adapter.find_by_order("att-1", datetime.now(UTC))

    assert result is None


# --- маскирование карты ---


def test_card_repr_hides_pan_and_cvc() -> None:
    text = repr(CardData("4111111111111111", "12/29", "123"))

    assert "4111111111111111" not in text
    assert "123" not in text
    assert "1111" in text

"""PallyAdapter: подпись постбэка, маппинг статусов, создание ссылки, ошибки API."""

import hashlib
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from app.core.config import PallySettings
from app.domain.enums import AttemptStatus, DeclineClass, PaymentMethod
from app.providers.base import ProviderRequest
from app.providers.pally import PallyAdapter, pally_signature

BASE_URL = "https://pally.test"
TOKEN = "tok"


@pytest.fixture
async def adapter() -> AsyncIterator[PallyAdapter]:
    a = PallyAdapter(
        "pally",
        PallySettings(base_url=BASE_URL, token=TOKEN, shop_id="SHOP"),
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
        "method": PaymentMethod.SBP,
        "customer_email": None,
        "return_url": "https://shop/return",
        "callback_url": "https://shop/cb",
        "link_ttl_seconds": 600,
    }
    values.update(overrides)
    return ProviderRequest(**values)


# --- подпись ---


def test_pally_signature_matches_md5_formula() -> None:
    expected = hashlib.md5(b"100.00:inv-1:tok").hexdigest().upper()

    assert pally_signature("100.00", "inv-1", "tok") == expected


def postback(
    out_sum: str = "100.00",
    inv_id: str = "inv-1",
    signature: str | None = None,
) -> dict[str, str]:
    if signature is None:
        signature = pally_signature(out_sum, inv_id, TOKEN)
    return {
        "OutSum": out_sum,
        "InvId": inv_id,
        "SignatureValue": signature,
        "Status": "SUCCESS",
    }


def test_verify_postback_accepts_valid_signature(adapter: PallyAdapter) -> None:
    assert adapter.verify_postback(postback()) is True


def test_verify_postback_accepts_lowercase_signature(adapter: PallyAdapter) -> None:
    form = postback()
    form["SignatureValue"] = form["SignatureValue"].lower()

    assert adapter.verify_postback(form) is True


def test_verify_postback_rejects_wrong_signature(adapter: PallyAdapter) -> None:
    form = postback(signature="DEADBEEF")

    assert adapter.verify_postback(form) is False


def test_verify_postback_rejects_empty_signature(adapter: PallyAdapter) -> None:
    form = postback(signature="")

    assert adapter.verify_postback(form) is False


# --- постбэк → ProviderResult ---


def test_postback_success_maps_to_succeeded_with_trs_id() -> None:
    form = postback() | {"TrsId": "TRS-42"}

    result = PallyAdapter.postback_to_result(form)

    assert result.status is AttemptStatus.SUCCEEDED
    assert result.provider_payment_id == "TRS-42"


def test_postback_overpaid_maps_to_succeeded() -> None:
    result = PallyAdapter.postback_to_result({"Status": "OVERPAID", "TrsId": "T1"})

    assert result.status is AttemptStatus.SUCCEEDED


def test_postback_fail_maps_to_declined() -> None:
    result = PallyAdapter.postback_to_result({"Status": "FAIL", "TrsId": "T1", "ErrorCode": "x"})

    assert result.status is AttemptStatus.DECLINED


def test_postback_underpaid_maps_to_hard_declined() -> None:
    result = PallyAdapter.postback_to_result({"Status": "UNDERPAID", "TrsId": "T1"})

    assert result.status is AttemptStatus.DECLINED
    assert result.decline_code == "underpaid"
    assert result.decline_class is DeclineClass.HARD


def test_postback_signature_not_leaked_into_raw() -> None:
    form = postback() | {"TrsId": "T1"}

    result = PallyAdapter.postback_to_result(form)

    assert "SignatureValue" not in result.raw
    assert "SignatureValue" not in str(result.raw)


# --- create_link ---


async def test_create_link_success(adapter: PallyAdapter) -> None:
    payload = {
        "success": "true",
        "link_url": "u",
        "link_page_url": "https://pay/x",
        "bill_id": "B1",
    }
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/v1/bill/create").mock(
            return_value=httpx.Response(200, json=payload)
        )

        result = await adapter.create_link(make_request())

    assert result.status is AttemptStatus.REQUIRES_ACTION
    assert result.provider_payment_id == "B1"
    assert result.next_action is not None
    assert result.next_action.url == "https://pay/x"

    request = route.calls.last.request
    form = parse_qs(request.content.decode())
    assert form["order_id"] == ["att-1"]
    assert form["deactivate_on_fail"] == ["1"]


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_class", "technical"),
    [
        (403, {"success": False, "error": "api:error.rate-not-found"}, DeclineClass.SOFT, False),
        (401, {"success": False, "error": "Unauthenticated"}, DeclineClass.CONFIG, False),
        (422, {"success": False}, DeclineClass.HARD, False),
        (500, {}, DeclineClass.SOFT, True),
    ],
)
async def test_create_link_error_responses(
    adapter: PallyAdapter,
    status_code: int,
    payload: dict[str, Any],
    expected_class: DeclineClass,
    technical: bool,
) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/v1/bill/create").mock(
            return_value=httpx.Response(status_code, json=payload)
        )

        result = await adapter.create_link(make_request())

    assert result.status is AttemptStatus.ERROR
    assert result.decline_class is expected_class
    assert result.technical_failure is technical


async def test_create_link_read_timeout_is_unknown(adapter: PallyAdapter) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/v1/bill/create").mock(side_effect=httpx.ReadTimeout("slow"))

        result = await adapter.create_link(make_request())

    assert result.status is AttemptStatus.UNKNOWN
    assert result.technical_failure is True


async def test_create_link_connect_error_is_soft_error(adapter: PallyAdapter) -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/v1/bill/create").mock(side_effect=httpx.ConnectError("no route"))

        result = await adapter.create_link(make_request())

    assert result.status is AttemptStatus.ERROR
    assert result.decline_class is DeclineClass.SOFT
    assert result.technical_failure is True

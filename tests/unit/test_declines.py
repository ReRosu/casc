"""Классификация кодов отказа провайдеров."""

import pytest

from app.domain.declines import (
    DEFAULT_CLASS,
    PALLY_ERROR_CODES,
    PAYMASTER_ERROR_CODES,
    PAYMASTER_RESULT_CODES,
    classify,
)
from app.domain.enums import DeclineClass


@pytest.mark.parametrize(
    "code",
    ["InsufficientFunds", "RejectedByFraud", "ThreeDSecureFailed", "InvalidAccount"],
)
def test_paymaster_result_codes_hard(code: str) -> None:
    assert classify(PAYMASTER_RESULT_CODES, code) is DeclineClass.HARD


@pytest.mark.parametrize(
    "code",
    ["TransactionDeclined", "IssuerUnavailable", "PaymentLimitExceeded"],
)
def test_paymaster_result_codes_soft(code: str) -> None:
    assert classify(PAYMASTER_RESULT_CODES, code) is DeclineClass.SOFT


def test_paymaster_error_not_authorized_is_config() -> None:
    assert classify(PAYMASTER_ERROR_CODES, "not_authorized") is DeclineClass.CONFIG


def test_paymaster_error_validation_is_hard() -> None:
    assert classify(PAYMASTER_ERROR_CODES, "validation_error") is DeclineClass.HARD


def test_paymaster_error_idempotency_violation_is_unknown() -> None:
    assert classify(PAYMASTER_ERROR_CODES, "idempotency_key_violation") is DeclineClass.UNKNOWN


@pytest.mark.parametrize("code", ["Unauthenticated", "api:error.ip_access_denied"])
def test_pally_error_config(code: str) -> None:
    assert classify(PALLY_ERROR_CODES, code) is DeclineClass.CONFIG


def test_pally_invalid_amount_is_hard() -> None:
    assert classify(PALLY_ERROR_CODES, "api:error.invalid_amount") is DeclineClass.HARD


def test_pally_rate_not_found_is_soft() -> None:
    assert classify(PALLY_ERROR_CODES, "api:error.rate-not-found") is DeclineClass.SOFT


def test_unknown_code_falls_back_to_default_soft() -> None:
    assert DEFAULT_CLASS is DeclineClass.SOFT
    assert classify(PAYMASTER_RESULT_CODES, "SomethingNobodyHeardOf") is DeclineClass.SOFT


def test_none_code_falls_back_to_default_soft() -> None:
    assert classify(PAYMASTER_RESULT_CODES, None) is DeclineClass.SOFT
    assert classify(PALLY_ERROR_CODES, None) is DeclineClass.SOFT

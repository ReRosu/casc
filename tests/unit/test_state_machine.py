"""Таблицы допустимых переходов статусов попытки и платежа."""

import pytest

from app.domain.enums import AttemptStatus, PaymentStatus
from app.domain.state_machine import can_attempt, can_payment


@pytest.mark.parametrize(
    ("src", "dst", "allowed"),
    [
        (AttemptStatus.PENDING, AttemptStatus.SUCCEEDED, True),
        (AttemptStatus.SUCCEEDED, AttemptStatus.DECLINED, False),
        # поздний успех: деньги списаны — фиксируем, даже если раньше был отказ
        (AttemptStatus.DECLINED, AttemptStatus.SUCCEEDED, True),
        (AttemptStatus.CANCELLED, AttemptStatus.SUCCEEDED, True),
        (AttemptStatus.UNKNOWN, AttemptStatus.DECLINED, True),
        # Запоздавший вебхук не должен «воскрешать» финальные статусы.
        (AttemptStatus.ERROR, AttemptStatus.SUCCEEDED, True),
        (AttemptStatus.DECLINED, AttemptStatus.ERROR, False),
        (AttemptStatus.REFUNDED, AttemptStatus.SUCCEEDED, False),
    ],
)
def test_can_attempt(src: AttemptStatus, dst: AttemptStatus, allowed: bool) -> None:
    assert can_attempt(src, dst) is allowed


def test_can_payment_late_success_after_failure() -> None:
    assert can_payment(PaymentStatus.FAILED, PaymentStatus.SUCCEEDED) is True


def test_can_payment_succeeded_cannot_fail() -> None:
    assert can_payment(PaymentStatus.SUCCEEDED, PaymentStatus.FAILED) is False


@pytest.mark.parametrize("dst", list(PaymentStatus))
def test_can_payment_refunded_is_terminal(dst: PaymentStatus) -> None:
    assert can_payment(PaymentStatus.REFUNDED, dst) is False


def test_can_payment_succeeded_can_be_refunded() -> None:
    assert can_payment(PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED) is True

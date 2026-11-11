"""Явные таблицы допустимых переходов. Всё, что не в таблице, — устаревшее или
дублирующееся событие, его игнорируем (вебхуки приходят повторно и не по порядку)."""

from app.domain.enums import AttemptStatus as A
from app.domain.enums import PaymentStatus as P

ATTEMPT_TRANSITIONS: dict[A, frozenset[A]] = {
    A.PENDING: frozenset({A.SUCCEEDED, A.REQUIRES_ACTION, A.DECLINED, A.ERROR, A.UNKNOWN}),
    A.UNKNOWN: frozenset({A.SUCCEEDED, A.REQUIRES_ACTION, A.DECLINED, A.ERROR, A.CANCELLED}),
    # UNKNOWN — таймаут при завершении 3DS (для hosted-ссылок движок такой переход игнорирует).
    A.REQUIRES_ACTION: frozenset({A.SUCCEEDED, A.DECLINED, A.CANCELLED, A.UNKNOWN}),
    # Поздний успех: провайдер сообщил, что деньги всё-таки списаны (погашенная ссылка,
    # «не создано» по нашему таймауту, гонка двух ответов 3DS). Деньги — факт, фиксируем
    # всегда, дальше движок либо засчитывает платёж, либо делает возврат дубля.
    A.CANCELLED: frozenset({A.SUCCEEDED}),
    A.DECLINED: frozenset({A.SUCCEEDED}),
    A.ERROR: frozenset({A.SUCCEEDED}),
    A.SUCCEEDED: frozenset({A.REFUNDED}),
    A.REFUNDED: frozenset(),
}

PAYMENT_TRANSITIONS: dict[P, frozenset[P]] = {
    P.CREATED: frozenset({P.PROCESSING, P.FAILED}),
    P.PROCESSING: frozenset({P.REQUIRES_ACTION, P.SUCCEEDED, P.FAILED}),
    P.REQUIRES_ACTION: frozenset({P.PROCESSING, P.SUCCEEDED, P.FAILED}),
    # Поздний успех по погашенной/«неизвестной» попытке: деньги списаны — платёж успешен.
    P.FAILED: frozenset({P.SUCCEEDED}),
    P.SUCCEEDED: frozenset({P.REFUNDED}),
    P.REFUNDED: frozenset(),
}

ATTEMPT_FINAL = frozenset({A.SUCCEEDED, A.DECLINED, A.ERROR, A.REFUNDED})
PAYMENT_FINAL = frozenset({P.SUCCEEDED, P.FAILED, P.REFUNDED})


def can_attempt(src: A, dst: A) -> bool:
    return dst in ATTEMPT_TRANSITIONS[src]


def can_payment(src: P, dst: P) -> bool:
    return dst in PAYMENT_TRANSITIONS[src]

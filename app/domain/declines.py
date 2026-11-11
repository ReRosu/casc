"""Классификация отказов провайдеров: HARD — стоп, SOFT — следующий провайдер, CONFIG — пропуск + алерт.

Принцип: HARD — если у другого провайдера с теми же данными исход будет тем же самым
(карта/запрос плохие), либо повтор вреден (фрод, клиент сам отменил). SOFT — если причина
в конкретном провайдере/эквайере/терминале.
"""

from app.domain.enums import DeclineClass

# PayMaster: справочник «Код авторизации» (resultCode).
PAYMASTER_RESULT_CODES: dict[str, DeclineClass] = {
    # Проблема карты или клиента — другой шлюз не поможет, а лишние попытки
    # увеличивают долю отказов мерчанта у эмитента.
    "InvalidAccount": DeclineClass.HARD,
    "BlockedAccount": DeclineClass.HARD,
    "ExpiredAccount": DeclineClass.HARD,
    "InsufficientFunds": DeclineClass.HARD,
    "RejectedByFraud": DeclineClass.HARD,
    "ThreeDSecureFailed": DeclineClass.HARD,
    "CancelledByUser": DeclineClass.HARD,
    "InvalidAmount": DeclineClass.HARD,
    "PaymentExpired": DeclineClass.HARD,
    # Превышено число попыток — сигнал прекратить ретраи.
    "PaymentCountExceeded": DeclineClass.HARD,
    # Проблема конкретного эквайера/терминала — другой шлюз может провести.
    "TransactionDeclined": DeclineClass.SOFT,  # общий отказ, аналог do_not_honor
    "IssuerUnavailable": DeclineClass.SOFT,
    "OperationNotAllowed": DeclineClass.SOFT,  # напр. терминал не принимает такой тип карт
    "PaymentLimitExceeded": DeclineClass.SOFT,  # лимит терминала/мерчанта у эквайера
    "CardNotEnrolled": DeclineClass.SOFT,  # другой шлюз может провести без 3DS
}

# PayMaster: справочник «Код ошибки» (поле code в ответе с ошибкой).
PAYMASTER_ERROR_CODES: dict[str, DeclineClass] = {
    "validation_error": DeclineClass.HARD,
    "not_authorized": DeclineClass.CONFIG,
    "invalid_operation": DeclineClass.SOFT,
    # Ключ уже использован с другим запросом: исход первого неизвестен — выясняем статус.
    "idempotency_key_violation": DeclineClass.UNKNOWN,
}

# Pally: ошибки API (ключ ошибки из ответа).
PALLY_ERROR_CODES: dict[str, DeclineClass] = {
    "Unauthenticated": DeclineClass.CONFIG,
    "api:error.merchant_banned": DeclineClass.CONFIG,
    "api:error.merchant_not_found": DeclineClass.CONFIG,
    "api:error.access_denied": DeclineClass.CONFIG,
    "api:error.ip_access_denied": DeclineClass.CONFIG,
    "api:error.invalid_amount": DeclineClass.HARD,
    "validation_error": DeclineClass.HARD,  # HTTP 422
    "api:error.rate-not-found": DeclineClass.SOFT,  # направление недоступно
    "api:error.transaction_rejected": DeclineClass.SOFT,
    "api:error.general_error": DeclineClass.SOFT,
}

# Неизвестный код безопаснее считать SOFT: следующий провайдер сам решит.
# Исключение — неизвестный отказ по карте после 3DS (см. engine), там каскад уже невозможен.
DEFAULT_CLASS = DeclineClass.SOFT


def classify(table: dict[str, DeclineClass], code: str | None) -> DeclineClass:
    if code is None:
        return DEFAULT_CLASS
    return table.get(code, DEFAULT_CLASS)

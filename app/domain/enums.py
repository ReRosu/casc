from enum import StrEnum


class Flow(StrEnum):
    # Каскад на этапе получения ссылки/реквизитов: клиента отправляем на страницу провайдера.
    LINK = "link"
    # Классический каскад: карта у нас на руках, пробуем провести через шлюзы по очереди.
    CARD = "card"


class ProviderMode(StrEnum):
    H2H = "h2h"  # host-to-host: мы сами передаём карту провайдеру
    HOSTED = "hosted"  # провайдер отдаёт ссылку на свою платёжную страницу


class Stage(StrEnum):
    LINK = "link"  # запрос ссылки/реквизитов, деньги ещё не двигались
    CHARGE = "charge"  # списание с карты


class PaymentMethod(StrEnum):
    BANKCARD = "bankcard"
    SBP = "sbp"


class PaymentStatus(StrEnum):
    CREATED = "created"
    PROCESSING = "processing"
    REQUIRES_ACTION = "requires_action"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class AttemptStatus(StrEnum):
    PENDING = "pending"  # запись создана, запрос к провайдеру в полёте
    REQUIRES_ACTION = "requires_action"  # 3DS или клиент на hosted-странице
    SUCCEEDED = "succeeded"
    DECLINED = "declined"  # провайдер ответил отказом
    ERROR = "error"  # технический сбой, запрос точно не исполнен
    UNKNOWN = "unknown"  # запрос ушёл, ответа нет: деньги могли списаться
    CANCELLED = "cancelled"  # ссылку погасили (осиротевший счёт, каскад ушёл дальше)
    REFUNDED = "refunded"


class DeclineClass(StrEnum):
    HARD = "hard"  # проблема в карте/запросе: у другого провайдера будет так же, стоп
    SOFT = "soft"  # проблема конкретного провайдера/эквайера: пробуем следующего
    CONFIG = "config"  # наша настройка у провайдера сломана: пропускаем его, алерт
    UNKNOWN = "unknown"  # исход неизвестен

from functools import lru_cache

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PayMasterSettings(BaseModel):
    base_url: str
    token: SecretStr
    merchant_id: str
    test_mode: bool = True


class PallySettings(BaseModel):
    base_url: str
    token: SecretStr
    shop_id: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://casc:casc@localhost:55432/casc"
    redis_url: str = "redis://localhost:56379/0"
    # Адрес, по которому провайдеры и браузер клиента достучатся до нашего сервиса
    # (callbackUrl, returnUrl, success_url/fail_url).
    public_base_url: str = "http://localhost:18000"
    # Подпись callback URL для провайдеров без подписи вебхуков (PayMaster).
    webhook_secret: SecretStr = SecretStr("dev-webhook-secret-change-me")

    # Каскад
    max_attempts: int = 3
    payment_deadline_seconds: int = 120
    link_ttl_seconds: int = 900
    # Продолжать каскад, если клиент получил отказ на hosted-странице провайдера.
    # По умолчанию нет: отказ скорее всего от эмитента, лишние попытки бьют по репутации.
    cascade_after_hosted_decline: bool = False

    # HTTP к провайдерам
    provider_connect_timeout: float = 3.0
    provider_read_timeout: float = 5.0

    # Разрешение UNKNOWN (таймаут после отправки запроса)
    unknown_inline_polls: int = 2
    unknown_inline_poll_interval: float = 1.0
    unknown_poll_max_attempts: int = 6
    unknown_poll_base_delay: float = 2.0
    # Сколько секунд «не найдено у провайдера» ещё не значит «не создано» (лаг поиска).
    # Если операция всё же появится позже — сработает логика позднего успеха.
    unknown_not_found_grace_seconds: float = 1.5

    # Circuit breaker
    cb_failure_threshold: int = 3
    cb_window_seconds: int = 60
    cb_open_seconds: int = 30

    paymaster_a: PayMasterSettings = PayMasterSettings(
        base_url="http://localhost:18001/paymaster-a",
        token=SecretStr("pm-a-test-token"),
        merchant_id="11111111-1111-1111-1111-111111111111",
    )
    paymaster_b: PayMasterSettings = PayMasterSettings(
        base_url="http://localhost:18001/paymaster-b",
        token=SecretStr("pm-b-test-token"),
        merchant_id="22222222-2222-2222-2222-222222222222",
    )
    pally: PallySettings = PallySettings(
        base_url="http://localhost:18001/pally",
        token=SecretStr("72|pally-test-token"),
        shop_id="LXZv3R7Q8B",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

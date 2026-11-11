from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class EmulatorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EMULATOR_", env_file=".env", extra="ignore")

    # Адрес эмулятора для браузера (ссылки на hosted-страницы и ACS).
    public_url: str = "http://localhost:18001"
    # Pally шлёт postback на Result URL из настроек магазина, а не из запроса.
    pally_result_url: str = "http://localhost:18000/webhooks/pally"
    # Подмена хостов для server-to-server колбэков (в docker: localhost:18000=api:8000).
    rewrite_hosts: str = ""
    # Сколько «висеть» в сценарии таймаута; должно быть больше read timeout клиента.
    timeout_sleep: float = 8.0

    paymaster_a_token: str = "pm-a-test-token"
    paymaster_b_token: str = "pm-b-test-token"
    pally_token: str = "72|pally-test-token"

    def rewrite(self, url: str) -> str:
        for pair in filter(None, self.rewrite_hosts.split(",")):
            src, dst = pair.split("=", 1)
            url = url.replace(src, dst)
        return url


@lru_cache
def get_emulator_settings() -> EmulatorSettings:
    return EmulatorSettings()

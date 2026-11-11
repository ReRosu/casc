from functools import lru_cache

from app.core.config import Settings, get_settings
from app.providers.base import ProviderAdapter
from app.providers.pally import PallyAdapter
from app.providers.paymaster import PayMasterAdapter


class ProviderRegistry:
    def __init__(self, adapters: dict[str, ProviderAdapter]) -> None:
        self._adapters = adapters

    def get(self, name: str) -> ProviderAdapter:
        return self._adapters[name]

    def names(self) -> list[str]:
        return list(self._adapters)

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()


def build_registry(settings: Settings) -> ProviderRegistry:
    timeouts = {"connect_timeout": settings.provider_connect_timeout,
                "read_timeout": settings.provider_read_timeout}
    return ProviderRegistry({
        # Два экземпляра PayMaster — как два эквайера/терминала у одного провайдера.
        "paymaster_a": PayMasterAdapter("paymaster_a", settings.paymaster_a, **timeouts),
        "paymaster_b": PayMasterAdapter("paymaster_b", settings.paymaster_b, **timeouts),
        "pally": PallyAdapter("pally", settings.pally, **timeouts),
    })


@lru_cache
def get_registry() -> ProviderRegistry:
    return build_registry(get_settings())


async def close_registry() -> None:
    if get_registry.cache_info().currsize:
        await get_registry().aclose()
    get_registry.cache_clear()

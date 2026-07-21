"""Device-authorization config factory registry (no OAuth HTTP imports).

Newly written for Accuretta. Kept separate from ``device_flow.py`` so provider
modules can register configs without circular imports through oauth_client.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from .device_models import DeviceAuthorizationConfig

_DEVICE_CONFIG_FACTORIES: Dict[str, Callable[[], DeviceAuthorizationConfig]] = {}


def register_device_config_factory(
    provider_id: str,
    factory: Callable[[], DeviceAuthorizationConfig],
) -> None:
    _DEVICE_CONFIG_FACTORIES[provider_id] = factory


def unregister_device_config_factory(provider_id: str) -> None:
    _DEVICE_CONFIG_FACTORIES.pop(provider_id, None)


def clear_device_config_factories() -> None:
    _DEVICE_CONFIG_FACTORIES.clear()


def resolve_device_config(provider_id: str) -> Optional[DeviceAuthorizationConfig]:
    factory = _DEVICE_CONFIG_FACTORIES.get(provider_id)
    if factory is None:
        return None
    return factory()

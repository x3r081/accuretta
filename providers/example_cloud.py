"""Disabled demonstration cloud provider (UI/API only).

Newly written for Accuretta. Performs no network I/O and has no OAuth
registration. Visible so the provider UI/API can be exercised safely.
"""

from __future__ import annotations

from .base import ApiMode, AuthType, ProviderCapabilities, ProviderDefinition
from .registry import ProviderRegistry, get_default_registry

EXAMPLE_CLOUD_DEFINITION = ProviderDefinition(
    id="example_cloud",
    display_name="Example cloud provider",
    api_mode=ApiMode.OPENAI_CHAT,
    auth_type=AuthType.OAUTH_PKCE,
    capabilities=ProviderCapabilities(
        streaming=True,
        tools=False,
        vision=False,
        cancellation=True,
        model_listing=False,
    ),
    default_base_url=None,
    supports_model_listing=False,
    experimental=True,
    enabled=False,
    disabled_reason="No provider registration configured",
)


def ensure_example_cloud_registered(registry: ProviderRegistry | None = None) -> None:
    reg = registry if registry is not None else get_default_registry()
    # Definition only — no factory, so get_provider() cannot invent a backend.
    reg.register(EXAMPLE_CLOUD_DEFINITION, factory=None)

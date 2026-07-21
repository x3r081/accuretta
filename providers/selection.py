"""Provider selection and chat-routing helpers.

Newly written for Accuretta. Local llama.cpp is always the default.
Legacy settings without provider_id resolve to local_llama.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

from .base import ProviderDefinition
from .errors import ProviderNotConfigured, ProviderUnavailable
from .registry import ProviderRegistry, get_default_registry

DEFAULT_PROVIDER_ID = "local_llama"


@dataclass
class ProviderSelection:
    provider_id: str
    definition: ProviderDefinition
    warning: Optional[str] = None
    fell_back: bool = False


def normalize_provider_id(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def resolve_provider_selection(
    settings: Optional[dict] = None,
    *,
    registry: Optional[ProviderRegistry] = None,
    requested_id: Optional[str] = None,
) -> ProviderSelection:
    """Resolve the effective provider for settings / chat.

    Policy:
    - missing / empty provider_id → local_llama (no warning)
    - unknown provider_id → fall back to local_llama with warning
    - known but disabled/unavailable → keep that id (caller blocks chat);
      selection APIs reject it separately
    """
    reg = registry or get_default_registry()
    raw = requested_id
    if raw is None and isinstance(settings, dict):
        raw = settings.get("provider_id")
    pid = normalize_provider_id(raw)
    if not pid:
        definition = reg.get_definition(DEFAULT_PROVIDER_ID)
        return ProviderSelection(provider_id=DEFAULT_PROVIDER_ID, definition=definition)

    try:
        definition = reg.get_definition(pid)
    except ProviderNotConfigured:
        definition = reg.get_definition(DEFAULT_PROVIDER_ID)
        return ProviderSelection(
            provider_id=DEFAULT_PROVIDER_ID,
            definition=definition,
            warning=f"Unknown provider {pid!r}; using Local llama.cpp",
            fell_back=True,
        )
    return ProviderSelection(provider_id=pid, definition=definition)


def assert_provider_usable_for_chat(selection: ProviderSelection) -> None:
    """Raise if the selected provider cannot serve chat in this phase."""
    d = selection.definition
    if not d.enabled:
        raise ProviderUnavailable(
            d.disabled_reason
            or f"Provider {d.display_name} is not available",
            provider_id=d.id,
        )
    if d.id != DEFAULT_PROVIDER_ID:
        raise ProviderUnavailable(
            f"Provider {d.display_name} is not available for chat yet",
            provider_id=d.id,
        )


def persist_provider_id(settings: dict, provider_id: str) -> dict:
    """Return a shallow-copied settings dict with provider_id set."""
    out = dict(settings)
    out["provider_id"] = provider_id
    return out

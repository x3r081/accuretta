"""Provider registry.

Newly written for Accuretta. Maps provider IDs to definitions and optional
factory callables without coupling chat execution to a specific backend.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from .base import InferenceProvider, ProviderDefinition
from .errors import ProviderNotConfigured

ProviderFactory = Callable[[], InferenceProvider]


class ProviderRegistry:
    def __init__(self) -> None:
        self._definitions: Dict[str, ProviderDefinition] = {}
        self._factories: Dict[str, ProviderFactory] = {}
        self._instances: Dict[str, InferenceProvider] = {}

    def register(
        self,
        definition: ProviderDefinition,
        factory: Optional[ProviderFactory] = None,
    ) -> None:
        if not definition.id:
            raise ValueError("provider id is required")
        self._definitions[definition.id] = definition
        if factory is not None:
            self._factories[definition.id] = factory
        # Drop cached instance if definition is replaced.
        self._instances.pop(definition.id, None)

    def unregister(self, provider_id: str) -> None:
        self._definitions.pop(provider_id, None)
        self._factories.pop(provider_id, None)
        self._instances.pop(provider_id, None)

    def get_definition(self, provider_id: str) -> ProviderDefinition:
        try:
            return self._definitions[provider_id]
        except KeyError as exc:
            raise ProviderNotConfigured(
                f"Unknown provider: {provider_id}",
                provider_id=provider_id,
            ) from exc

    def list_definitions(self, *, include_disabled: bool = False) -> List[ProviderDefinition]:
        defs = list(self._definitions.values())
        if not include_disabled:
            defs = [d for d in defs if d.enabled]
        return sorted(defs, key=lambda d: (d.experimental, d.display_name.lower()))

    def get_provider(self, provider_id: str) -> InferenceProvider:
        if provider_id in self._instances:
            return self._instances[provider_id]
        definition = self.get_definition(provider_id)
        factory = self._factories.get(provider_id)
        if factory is None:
            raise ProviderNotConfigured(
                f"Provider {provider_id!r} has no runtime factory",
                provider_id=provider_id,
            )
        if not definition.enabled:
            raise ProviderNotConfigured(
                f"Provider {provider_id!r} is disabled",
                provider_id=provider_id,
            )
        instance = factory()
        self._instances[provider_id] = instance
        return instance

    def ids(self) -> Iterable[str]:
        return tuple(self._definitions.keys())


_DEFAULT_REGISTRY: Optional[ProviderRegistry] = None


def get_default_registry() -> ProviderRegistry:
    """Lazy singleton so importing providers does not force llama wiring."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ProviderRegistry()
        _register_builtins(_DEFAULT_REGISTRY)
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Test helper — clear the singleton."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None


def _register_builtins(registry: ProviderRegistry) -> None:
    # Import locally to attach definition + factory without circular imports
    # at module load of registry.py.
    from .example_cloud import ensure_example_cloud_registered
    from .local_llama import ensure_local_llama_registered
    from .openai_provider import ensure_openai_registered

    ensure_local_llama_registered(registry)
    ensure_example_cloud_registered(registry)
    ensure_openai_registered(registry)

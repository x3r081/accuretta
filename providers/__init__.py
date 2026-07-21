"""Inference provider abstractions for Accuretta.

Newly written for Accuretta. Local llama.cpp remains the default backend;
cloud providers plug in later behind the same interfaces.
"""

from .base import (
    ApiMode,
    AuthType,
    InferenceEvent,
    InferenceEventType,
    InferenceProvider,
    InferenceRequest,
    ProviderCapabilities,
    ProviderDefinition,
    RuntimeCredentials,
)
from .errors import (
    AuthenticationCancelled,
    AuthenticationExpired,
    AuthenticationRequired,
    InvalidProviderResponse,
    OAuthStateMismatch,
    ProviderError,
    ProviderNotConfigured,
    ProviderUnavailable,
    RateLimited,
    TokenExchangeFailed,
    TokenRefreshFailed,
)
from .example_cloud import EXAMPLE_CLOUD_DEFINITION, ensure_example_cloud_registered
from .local_llama import LOCAL_LLAMA_DEFINITION, LocalLlamaProvider, ensure_local_llama_registered
from .registry import ProviderRegistry, get_default_registry
from .selection import DEFAULT_PROVIDER_ID, resolve_provider_selection
from .status import assert_safe_provider_payload, build_safe_provider_status

__all__ = [
    "ApiMode",
    "AuthType",
    "AuthenticationCancelled",
    "AuthenticationExpired",
    "AuthenticationRequired",
    "DEFAULT_PROVIDER_ID",
    "EXAMPLE_CLOUD_DEFINITION",
    "InferenceEvent",
    "InferenceEventType",
    "InferenceProvider",
    "InferenceRequest",
    "InvalidProviderResponse",
    "LOCAL_LLAMA_DEFINITION",
    "LocalLlamaProvider",
    "OAuthStateMismatch",
    "ProviderCapabilities",
    "ProviderDefinition",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderRegistry",
    "ProviderUnavailable",
    "RateLimited",
    "RuntimeCredentials",
    "TokenExchangeFailed",
    "TokenRefreshFailed",
    "assert_safe_provider_payload",
    "build_safe_provider_status",
    "ensure_example_cloud_registered",
    "ensure_local_llama_registered",
    "get_default_registry",
    "resolve_provider_selection",
]

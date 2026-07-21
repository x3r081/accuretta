"""Inference provider abstractions for Accuretta.

Newly written for Accuretta. Local llama.cpp remains the default backend;
cloud providers plug in later behind the same interfaces.
"""

from .local_llama import LocalLlamaProvider, LOCAL_LLAMA_DEFINITION, ensure_local_llama_registered
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
from .registry import ProviderRegistry, get_default_registry

__all__ = [
    "ApiMode",
    "AuthType",
    "AuthenticationCancelled",
    "AuthenticationExpired",
    "AuthenticationRequired",
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
    "ensure_local_llama_registered",
    "get_default_registry",
]

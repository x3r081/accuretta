"""Typed provider and inference interfaces.

Newly written for Accuretta. Keep abstractions thin — only what the app
needs for local llama.cpp today and authenticated cloud providers later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Optional, Protocol, Sequence, runtime_checkable


class ApiMode(str, Enum):
    """Wire protocol used for inference."""

    OPENAI_CHAT = "openai_chat"
    LOCAL_LLAMA = "local_llama"


class AuthType(str, Enum):
    """How credentials are acquired for a provider."""

    NONE = "none"
    API_KEY = "api_key"
    OAUTH_PKCE = "oauth_pkce"
    OAUTH_DEVICE = "oauth_device"


class InferenceEventType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    USAGE = "usage"
    ERROR = "error"
    COMPLETED = "completed"


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool = True
    tools: bool = True
    vision: bool = False
    cancellation: bool = True
    model_listing: bool = True
    account_authentication: bool = False
    device_authorization: bool = False
    inference: bool = True


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    display_name: str
    api_mode: ApiMode
    auth_type: AuthType
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    default_base_url: Optional[str] = None
    supports_model_listing: bool = True
    supports_inference: bool = True
    experimental: bool = False
    enabled: bool = True
    # Human-readable reason when enabled=False (shown in UI; never secrets).
    disabled_reason: Optional[str] = None


@dataclass
class RuntimeCredentials:
    """Short-lived credential supplied to an inference request.

    Never serialize this object into frontend responses or logs.
    """

    provider_id: str
    access_token: Optional[str] = None
    expires_at: Optional[float] = None  # unix epoch seconds
    base_url: Optional[str] = None
    source: str = "none"  # none | keychain | file | env | local


@dataclass
class InferenceRequest:
    model: str
    messages: list[dict[str, Any]]
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    tools: Optional[list[dict[str, Any]]] = None
    cancellation_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class InferenceEvent:
    event_type: InferenceEventType
    text_delta: Optional[str] = None
    tool_call_delta: Optional[dict[str, Any]] = None
    usage: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    completion_reason: Optional[str] = None
    raw: Optional[dict[str, Any]] = None


@runtime_checkable
class InferenceProvider(Protocol):
    """Sends requests, streams responses, cancels work, reports errors."""

    @property
    def definition(self) -> ProviderDefinition: ...

    def validate_configuration(self) -> None: ...

    def list_models(self) -> Sequence[dict[str, Any]]: ...

    def stream_response(
        self,
        request: InferenceRequest,
        credentials: Optional[RuntimeCredentials] = None,
    ) -> Iterator[InferenceEvent]: ...

    def cancel(self, cancellation_id: str) -> bool: ...

    def capabilities(self) -> ProviderCapabilities: ...

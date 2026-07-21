"""Runtime credential resolution.

Newly written for Accuretta. Resolves which provider/credentials to use for
an inference request without sending tokens to the frontend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .base import ProviderDefinition, RuntimeCredentials
from .errors import AuthenticationExpired, AuthenticationRequired, ProviderNotConfigured
from .registry import ProviderRegistry


class CredentialSource(Protocol):
    """Minimal auth-store surface used by the runtime resolver."""

    def load(self, provider_id: str) -> Optional[object]: ...


@dataclass
class ResolvedRuntime:
    provider_id: str
    definition: ProviderDefinition
    credentials: RuntimeCredentials


@dataclass
class RuntimeResolver:
    """Choose a provider and attach non-expired runtime credentials."""

    registry: ProviderRegistry
    credential_source: Optional[CredentialSource] = None
    default_provider_id: str = "local_llama"
    refresh_skew_seconds: int = 120
    clock: Optional[Callable[[], float]] = None

    def resolve(
        self,
        provider_id: Optional[str] = None,
        *,
        require_auth: bool = False,
    ) -> ResolvedRuntime:
        import time

        now = (self.clock or time.time)()
        pid = (provider_id or self.default_provider_id).strip() or self.default_provider_id
        definition = self.registry.get_definition(pid)

        if definition.auth_type.value == "none":
            return ResolvedRuntime(
                provider_id=pid,
                definition=definition,
                credentials=RuntimeCredentials(
                    provider_id=pid,
                    access_token=None,
                    expires_at=None,
                    base_url=definition.default_base_url,
                    source="local",
                ),
            )

        if self.credential_source is None:
            if require_auth:
                raise AuthenticationRequired(
                    f"Authentication required for provider {pid}",
                    provider_id=pid,
                )
            raise ProviderNotConfigured(
                f"No credential source configured for provider {pid}",
                provider_id=pid,
            )

        stored = self.credential_source.load(pid)
        if stored is None:
            raise AuthenticationRequired(
                f"Not authenticated with provider {pid}",
                provider_id=pid,
            )

        access_token = getattr(stored, "access_token", None)
        expires_at = getattr(stored, "expires_at", None)
        base_url = getattr(stored, "base_url", None) or definition.default_base_url
        source = getattr(stored, "source", "store")

        if not access_token:
            raise AuthenticationRequired(
                f"Stored credentials for {pid} are incomplete",
                provider_id=pid,
            )

        if expires_at is not None:
            try:
                exp = float(expires_at)
            except (TypeError, ValueError):
                raise AuthenticationExpired(
                    f"Stored credentials for {pid} have an invalid expiry",
                    provider_id=pid,
                )
            if exp <= (now + self.refresh_skew_seconds):
                raise AuthenticationExpired(
                    f"Credentials for {pid} are expired or within refresh skew",
                    provider_id=pid,
                )

        return ResolvedRuntime(
            provider_id=pid,
            definition=definition,
            credentials=RuntimeCredentials(
                provider_id=pid,
                access_token=access_token,
                expires_at=float(expires_at) if expires_at is not None else None,
                base_url=base_url,
                source=str(source),
            ),
        )


def safe_provider_status(
    definition: ProviderDefinition,
    *,
    authenticated: bool = False,
    expires_at: Optional[str] = None,
    account_label: Optional[str] = None,
    error: Optional[str] = None,
    available: bool = True,
    selected: bool = False,
    is_default: bool = False,
    disabled_reason: Optional[str] = None,
) -> dict:
    """Frontend-safe status payload — never includes tokens."""
    from .status import build_safe_provider_status

    return build_safe_provider_status(
        definition,
        authenticated=authenticated,
        available=available,
        selected=selected,
        is_default=is_default,
        expires_at=expires_at,
        account_label=account_label,
        disabled_reason=disabled_reason,
        error=error,
    )

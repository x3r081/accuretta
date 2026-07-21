"""Classify OAuth token-refresh failures.

Newly written for Accuretta. Only permanent credential rejection should
delete stored credentials. Transient and configuration failures retain them.
"""

from __future__ import annotations

import socket
import urllib.error
from enum import Enum
from typing import Any, Optional

from providers.errors import (
    AuthenticationRequired,
    ProviderNotConfigured,
    TokenRefreshFailed,
)


class RefreshFailureKind(str, Enum):
    PERMANENT = "permanent"
    TRANSIENT = "transient"
    CONFIGURATION = "configuration"


# OAuth error codes that mean the stored refresh credential is unusable.
_PERMANENT_OAUTH_ERRORS = frozenset({
    "invalid_grant",
    "invalid_token",
    "revoked",
    "token_revoked",
    "expired_token",
    "invalid_client",
})


def _oauth_error_from_payload(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, str) and err.strip():
        return err.strip().lower()
    return None


def classify_refresh_http_failure(
    *,
    status_code: Optional[int] = None,
    oauth_error: Optional[str] = None,
    transport_exc: Optional[BaseException] = None,
) -> RefreshFailureKind:
    """Classify a refresh failure without inspecting secret-bearing bodies."""
    err = (oauth_error or "").strip().lower()
    if err in _PERMANENT_OAUTH_ERRORS:
        return RefreshFailureKind.PERMANENT

    if transport_exc is not None:
        if isinstance(transport_exc, (TimeoutError, socket.timeout)):
            return RefreshFailureKind.TRANSIENT
        if isinstance(transport_exc, urllib.error.URLError):
            return RefreshFailureKind.TRANSIENT
        if isinstance(transport_exc, (ConnectionError, OSError)):
            return RefreshFailureKind.TRANSIENT

    if status_code is None:
        return RefreshFailureKind.TRANSIENT
    if status_code in (401, 403) and err in _PERMANENT_OAUTH_ERRORS:
        return RefreshFailureKind.PERMANENT
    if status_code == 400 and err in _PERMANENT_OAUTH_ERRORS:
        return RefreshFailureKind.PERMANENT
    # 401/403 without a known permanent oauth error: treat as permanent only
    # when the provider explicitly used a permanent code; otherwise transient
    # (e.g. misconfigured gateway) — but invalid_client/invalid_grant already
    # covered. Bare 401 on refresh usually means the RT is dead.
    if status_code == 401 and not err:
        return RefreshFailureKind.PERMANENT
    if status_code == 429:
        return RefreshFailureKind.TRANSIENT
    if 500 <= int(status_code) <= 599:
        return RefreshFailureKind.TRANSIENT
    if status_code in (408, 425, 502, 503, 504):
        return RefreshFailureKind.TRANSIENT
    # Other 4xx without permanent oauth code → treat as transient to avoid
    # wiping credentials on unexpected provider quirks; callers may still
    # refuse to use an expired access token.
    if 400 <= int(status_code) < 500 and err in _PERMANENT_OAUTH_ERRORS:
        return RefreshFailureKind.PERMANENT
    return RefreshFailureKind.TRANSIENT


def refresh_failure(
    message: str,
    *,
    provider_id: Optional[str] = None,
    kind: RefreshFailureKind = RefreshFailureKind.TRANSIENT,
    oauth_error: Optional[str] = None,
    status_code: Optional[int] = None,
) -> TokenRefreshFailed:
    retryable = kind != RefreshFailureKind.PERMANENT
    if kind == RefreshFailureKind.CONFIGURATION:
        retryable = False
    return TokenRefreshFailed(
        message,
        provider_id=provider_id,
        kind=kind.value,
        retryable=retryable,
        oauth_error=oauth_error,
        status_code=status_code,
    )


def should_delete_credentials(exc: BaseException) -> bool:
    """True only for definitive credential rejection or unusable local data."""
    if isinstance(exc, AuthenticationRequired):
        return True
    if isinstance(exc, TokenRefreshFailed):
        return exc.kind == RefreshFailureKind.PERMANENT.value
    return False


def is_configuration_error(exc: BaseException) -> bool:
    if isinstance(exc, ProviderNotConfigured):
        return True
    if isinstance(exc, TokenRefreshFailed):
        return exc.kind == RefreshFailureKind.CONFIGURATION.value
    return False

"""Typed models for OAuth 2.0 device authorization (RFC 8628).

Newly written for Accuretta. Provider-configured — no GitHub-specific fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional


class DeviceFlowStatus(str, Enum):
    IDLE = "idle"
    PENDING = "pending"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class DeviceAuthorizationConfig:
    """Provider-supplied device-authorization endpoints and client registration.

    client_id must be Accuretta's own OAuth App registration.
    """

    provider_id: str
    client_id: str
    device_authorization_endpoint: str
    token_endpoint: str
    scopes: tuple[str, ...] = ()
    audience: Optional[str] = None
    extra_authorization_parameters: Mapping[str, str] = field(default_factory=dict)
    extra_token_parameters: Mapping[str, str] = field(default_factory=dict)
    client_secret: Optional[str] = None  # public device clients usually omit
    min_poll_interval_seconds: int = 5
    max_poll_interval_seconds: int = 30


@dataclass
class DeviceAuthorizationSession:
    """In-flight device session. device_code must never leave the backend."""

    provider_id: str
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: Optional[str]
    expires_at: float  # unix epoch
    interval: float
    generation: int = 0
    status: DeviceFlowStatus = DeviceFlowStatus.PENDING
    error: Optional[str] = None
    # Opaque metadata for the provider after success (never tokens).
    provider_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceAuthorizationResult:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None
    token_type: str = "Bearer"
    scopes: tuple[str, ...] = ()
    provider_metadata: Dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"DeviceAuthorizationResult(token_type={self.token_type!r}, "
            f"has_access_token={bool(self.access_token)}, "
            f"has_refresh_token={bool(self.refresh_token)}, "
            f"expires_at={self.expires_at!r}, scopes={self.scopes!r})"
        )


def safe_device_start_payload(session: DeviceAuthorizationSession) -> dict:
    """Frontend-safe start/status fields — never includes device_code or tokens."""
    from datetime import datetime, timezone

    expires_iso = datetime.fromtimestamp(session.expires_at, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    out = {
        "providerId": session.provider_id,
        "status": session.status.value,
        "userCode": session.user_code,
        "verificationUri": session.verification_uri,
        "expiresAt": expires_iso,
        "pollIntervalSeconds": int(max(1, round(session.interval))),
    }
    if session.verification_uri_complete:
        out["verificationUriComplete"] = session.verification_uri_complete
    if session.error:
        out["error"] = session.error
    return out


def safe_device_status_payload(
    provider_id: str,
    *,
    status: DeviceFlowStatus,
    session: Optional[DeviceAuthorizationSession] = None,
    error: Optional[str] = None,
) -> dict:
    if session is not None and status == DeviceFlowStatus.PENDING:
        return safe_device_start_payload(session)
    out = {
        "providerId": provider_id,
        "status": status.value,
    }
    if error:
        out["error"] = error
    elif session is not None and session.error:
        out["error"] = session.error
    # When pending-equivalent info is useful after cancel/expiry, omit codes.
    return out

"""Token expiry and refresh orchestration.

Adapted from Nous Research Hermes Agent (MIT) expiry helpers
(``_is_expiring``, ``_coerce_ttl_seconds``, ``_parse_iso_timestamp``)
and refresh persistence patterns. See THIRD_PARTY_NOTICES.md.

Failed refresh must not silently continue with stale access tokens.
Only permanent credential rejection deletes stored credentials.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from providers.errors import (
    AuthenticationExpired,
    AuthenticationRequired,
    ProviderNotConfigured,
    TokenRefreshFailed,
)

from .models import StoredCredential
from .oauth_client import OAuthProviderConfig, TokenResponse, refresh_access_token
from .refresh_errors import (
    RefreshFailureKind,
    refresh_failure,
    should_delete_credentials,
)
from .store import AuthStore

DEFAULT_REFRESH_SKEW_SECONDS = 120


def parse_iso_timestamp(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def coerce_ttl_seconds(expires_in: Any) -> int:
    try:
        ttl = int(expires_in)
    except Exception:
        ttl = 0
    return max(0, ttl)


def expires_at_from_ttl(expires_in: Any, *, now: Optional[float] = None) -> float:
    n = time.time() if now is None else float(now)
    return n + coerce_ttl_seconds(expires_in)


def is_expiring(
    expires_at: Any,
    *,
    skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """True when credential is missing expiry or within skew of expiry."""
    n = time.time() if now is None else float(now)
    if expires_at is None:
        return True
    if isinstance(expires_at, str):
        epoch = parse_iso_timestamp(expires_at)
    else:
        try:
            epoch = float(expires_at)
        except (TypeError, ValueError):
            return True
    if epoch is None:
        return True
    return epoch <= (n + max(0, int(skew_seconds)))


def apply_token_response(
    provider_id: str,
    token: TokenResponse,
    *,
    previous: Optional[StoredCredential] = None,
    now: Optional[float] = None,
) -> StoredCredential:
    """Build a StoredCredential from a token response.

    Persists rotated refresh tokens when the provider returns a new one.
    """
    n = time.time() if now is None else float(now)
    refresh = token.refresh_token
    if refresh is None and previous is not None:
        refresh = previous.refresh_token
    scopes = []
    if token.scope:
        scopes = [s for s in token.scope.replace(",", " ").split() if s]
    elif previous is not None:
        scopes = list(previous.scopes)
    metadata = dict(previous.metadata) if previous is not None else {}
    expires_at = None
    if token.expires_in is not None:
        expires_at = expires_at_from_ttl(token.expires_in, now=n)
    elif previous is not None:
        expires_at = previous.expires_at
    return StoredCredential(
        provider_id=provider_id,
        access_token=token.access_token,
        refresh_token=refresh,
        expires_at=expires_at,
        token_type=token.token_type or "Bearer",
        scopes=scopes,
        metadata=metadata,
    )


def ensure_fresh_credential(
    store: AuthStore,
    config: OAuthProviderConfig,
    *,
    skew_seconds: int = DEFAULT_REFRESH_SKEW_SECONDS,
    now: Optional[float] = None,
    refresher: Optional[Callable[..., TokenResponse]] = None,
) -> StoredCredential:
    """Load credential and refresh if within skew.

    - Permanent refresh rejection → delete credentials, raise AuthenticationRequired
    - Transient failure → retain credentials, raise retryable TokenRefreshFailed
      (never continue with an already-expired access token)
    - Configuration failure → retain credentials, raise ProviderNotConfigured
    """
    n = time.time() if now is None else float(now)

    if not (config.token_url or "").strip() or not (config.client_id or "").strip():
        # Keep any stored credential; this is a developer/config problem.
        raise ProviderNotConfigured(
            "OAuth provider is missing token_url or client_id",
            provider_id=config.provider_id,
        )

    try:
        cred = store.load(config.provider_id)
    except ValueError:
        # Malformed local credential — remove unusable data.
        try:
            store.delete(config.provider_id)
        except Exception:
            pass
        raise AuthenticationRequired(
            f"Stored credentials for {config.provider_id} are invalid",
            provider_id=config.provider_id,
        ) from None

    if cred is None:
        raise AuthenticationExpired(
            f"No credentials for {config.provider_id}",
            provider_id=config.provider_id,
        )
    if not cred.access_token:
        store.delete(config.provider_id)
        raise AuthenticationRequired(
            f"Stored credentials for {config.provider_id} are incomplete",
            provider_id=config.provider_id,
        )

    if not is_expiring(cred.expires_at, skew_seconds=skew_seconds, now=n):
        return cred

    if not cred.refresh_token:
        # Access token expired/within skew and no RT — credential unusable.
        store.delete(config.provider_id)
        raise AuthenticationRequired(
            f"Credentials for {config.provider_id} expired and no refresh token is available",
            provider_id=config.provider_id,
        )

    refresh_fn = refresher or refresh_access_token
    try:
        token = refresh_fn(config, refresh_token=cred.refresh_token)
    except ProviderNotConfigured:
        raise
    except TokenRefreshFailed as exc:
        if should_delete_credentials(exc):
            store.delete(config.provider_id)
            raise AuthenticationRequired(
                f"Credentials for {config.provider_id} were rejected; reconnect required",
                provider_id=config.provider_id,
            ) from None
        # Transient / configuration-classified TokenRefreshFailed: retain store.
        # Do not return the expired access token for inference.
        raise
    except Exception:
        # Unknown failure — treat as transient; retain credentials.
        raise refresh_failure(
            f"Refresh failed for {config.provider_id}",
            provider_id=config.provider_id,
            kind=RefreshFailureKind.TRANSIENT,
        ) from None

    fresh = apply_token_response(config.provider_id, token, previous=cred, now=n)
    if not fresh.access_token:
        # Do not overwrite store with incomplete data.
        raise refresh_failure(
            "Refresh returned an incomplete token response",
            provider_id=config.provider_id,
            kind=RefreshFailureKind.TRANSIENT,
        )
    store.save(config.provider_id, fresh)
    return fresh

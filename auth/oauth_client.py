"""Generic OAuth client driven by provider configuration.

Adapted from Nous Research Hermes Agent (MIT) authorization URL assembly,
code exchange, and device-code polling primitives. Accuretta-specific:
configuration objects instead of provider if/else trees; no Hermes client
IDs or provider registrations. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Union

from providers.errors import (
    TokenExchangeFailed,
    TokenRefreshFailed,
)

from .pkce import PkcePair, generate_state, pkce_pair
from .redact import redact_sensitive_text


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Provider-supplied OAuth endpoints and client registration.

    client_id / redirect_uri / scopes must be Accuretta's own registration
    (or user-supplied). Do not reuse Hermes client IDs.
    """

    provider_id: str
    authorize_url: str
    token_url: str
    client_id: str
    scopes: tuple[str, ...] = ()
    client_secret: Optional[str] = None  # public PKCE clients omit this
    extra_authorize_params: Mapping[str, str] = field(default_factory=dict)
    device_code_url: Optional[str] = None


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    token_type: str = "Bearer"
    scope: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


def build_authorize_url(
    config: OAuthProviderConfig,
    *,
    redirect_uri: str,
    state: str,
    pkce: PkcePair,
) -> str:
    params = {
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": pkce.method,
    }
    if config.scopes:
        params["scope"] = " ".join(config.scopes)
    for key, value in config.extra_authorize_params.items():
        params[str(key)] = str(value)
    return f"{config.authorize_url}?{urllib.parse.urlencode(params)}"


def _post_form(
    url: str,
    data: Dict[str, str],
    *,
    timeout: float = 30.0,
    return_error_payload: bool = False,
) -> Dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        oauth_error = None
        parsed: Dict[str, Any] = {}
        try:
            parsed = json.loads(detail) if detail else {}
            if isinstance(parsed, dict):
                oauth_error = parsed.get("error")
                if isinstance(oauth_error, str):
                    oauth_error = oauth_error.strip().lower()
                else:
                    oauth_error = None
            else:
                parsed = {}
        except Exception:
            parsed = {}
        if return_error_payload and isinstance(parsed, dict) and parsed.get("error"):
            # RFC 8628 pending/slow_down/denied arrive as HTTP 400 JSON bodies.
            return {
                "error": str(parsed.get("error") or ""),
                "error_description": parsed.get("error_description"),
                "_http_status": int(exc.code),
            }
        # Do not include response bodies in the exception message.
        raise TokenExchangeFailed(
            redact_sensitive_text(f"token endpoint HTTP {exc.code}"),
            provider_id=None,
        ) from _TokenEndpointHTTPError(exc.code, oauth_error, parsed if isinstance(parsed, dict) else {})
    except Exception as exc:
        raise TokenExchangeFailed(
            redact_sensitive_text(f"token endpoint request failed: {type(exc).__name__}")
        ) from exc

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise TokenExchangeFailed("token endpoint returned non-JSON body") from exc
    if not isinstance(payload, dict):
        raise TokenExchangeFailed("token endpoint returned invalid JSON")
    if return_error_payload:
        payload = dict(payload)
        payload["_http_status"] = int(status)
    return payload


class _TokenEndpointHTTPError(Exception):
    """Internal carrier for HTTP status / oauth error code (no secrets)."""

    def __init__(self, status_code: int, oauth_error: Optional[str], payload: Dict[str, Any]):
        super().__init__(f"HTTP {status_code}")
        self.status_code = int(status_code)
        self.oauth_error = oauth_error
        # Keep only the error code fields — never tokens.
        self.safe_payload = {
            k: payload.get(k)
            for k in ("error", "error_description")
            if k in payload and k == "error"
        }


def parse_token_response(payload: Mapping[str, Any]) -> TokenResponse:
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise TokenExchangeFailed("token response missing access_token")
    refresh = payload.get("refresh_token")
    if refresh is not None and not isinstance(refresh, str):
        raise TokenExchangeFailed("token response has invalid refresh_token")
    expires_in = payload.get("expires_in")
    if expires_in is not None:
        try:
            expires_in = int(expires_in)
        except (TypeError, ValueError) as exc:
            raise TokenExchangeFailed("token response has invalid expires_in") from exc
    token_type = payload.get("token_type") or "Bearer"
    scope = payload.get("scope")
    # Strip internal markers.
    raw = {k: v for k, v in dict(payload).items() if not str(k).startswith("_")}
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        token_type=str(token_type),
        scope=str(scope) if scope is not None else None,
        raw=raw,
    )


def exchange_authorization_code(
    config: OAuthProviderConfig,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    timeout: float = 30.0,
) -> TokenResponse:
    data = {
        "grant_type": "authorization_code",
        "client_id": config.client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if config.client_secret:
        data["client_secret"] = config.client_secret
    payload = _post_form(config.token_url, data, timeout=timeout)
    if payload.get("error"):
        raise TokenExchangeFailed(
            redact_sensitive_text(str(payload.get("error_description") or payload.get("error")))
        )
    return parse_token_response(payload)


def refresh_access_token(
    config: OAuthProviderConfig,
    *,
    refresh_token: str,
    timeout: float = 30.0,
) -> TokenResponse:
    from .refresh_errors import (
        RefreshFailureKind,
        classify_refresh_http_failure,
        refresh_failure,
    )

    if not (config.token_url or "").strip() or not (config.client_id or "").strip():
        raise refresh_failure(
            "OAuth token endpoint or client_id is not configured",
            provider_id=config.provider_id,
            kind=RefreshFailureKind.CONFIGURATION,
        )
    if not refresh_token:
        raise refresh_failure(
            "No refresh token available",
            provider_id=config.provider_id,
            kind=RefreshFailureKind.PERMANENT,
        )

    data = {
        "grant_type": "refresh_token",
        "client_id": config.client_id,
        "refresh_token": refresh_token,
    }
    if config.client_secret:
        data["client_secret"] = config.client_secret
    try:
        payload = _post_form(config.token_url, data, timeout=timeout)
    except TokenExchangeFailed as exc:
        cause = exc.__cause__
        status_code = getattr(cause, "status_code", None) if cause is not None else None
        oauth_error = getattr(cause, "oauth_error", None) if cause is not None else None
        kind = classify_refresh_http_failure(
            status_code=status_code,
            oauth_error=oauth_error,
            transport_exc=cause if not isinstance(cause, _TokenEndpointHTTPError) else None,
        )
        # URLError / timeout wrapped as TokenExchangeFailed without HTTP carrier
        if cause is not None and not isinstance(cause, _TokenEndpointHTTPError):
            kind = classify_refresh_http_failure(transport_exc=cause)
        raise refresh_failure(
            "Token refresh failed",
            provider_id=config.provider_id,
            kind=kind,
            oauth_error=oauth_error,
            status_code=status_code,
        ) from None

    if payload.get("error"):
        oauth_error = str(payload.get("error") or "").strip().lower()
        kind = classify_refresh_http_failure(oauth_error=oauth_error, status_code=400)
        raise refresh_failure(
            "Token refresh rejected",
            provider_id=config.provider_id,
            kind=kind,
            oauth_error=oauth_error,
            status_code=400,
        )
    try:
        return parse_token_response(payload)
    except TokenExchangeFailed:
        # Malformed success body — transient; do not wipe credentials.
        raise refresh_failure(
            "Token refresh returned an invalid response",
            provider_id=config.provider_id,
            kind=RefreshFailureKind.TRANSIENT,
        ) from None


@dataclass
class AuthorizationSession:
    config: OAuthProviderConfig
    redirect_uri: str
    state: str
    pkce: PkcePair
    authorize_url: str


def begin_authorization(
    config: OAuthProviderConfig,
    *,
    redirect_uri: str,
    state: Optional[str] = None,
    pkce: Optional[PkcePair] = None,
) -> AuthorizationSession:
    pair = pkce or pkce_pair()
    st = state or generate_state()
    url = build_authorize_url(config, redirect_uri=redirect_uri, state=st, pkce=pair)
    return AuthorizationSession(
        config=config,
        redirect_uri=redirect_uri,
        state=st,
        pkce=pair,
        authorize_url=url,
    )


def poll_device_code_token(
    *,
    token_url: str,
    client_id: str,
    device_code: str,
    expires_in: int,
    interval: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    post_form: Callable[[str, Dict[str, str]], Dict[str, Any]],
    interval_cap: int = 30,
) -> TokenResponse:
    """RFC 8628 device-code polling primitive (provider-agnostic)."""
    deadline = monotonic() + max(1, int(expires_in))
    current_interval = max(1, min(int(interval), interval_cap))
    while monotonic() < deadline:
        payload = post_form(
            token_url,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device_code,
            },
        )
        if payload.get("access_token"):
            return parse_token_response(payload)
        error = payload.get("error") or ""
        if error == "authorization_pending":
            sleep(current_interval)
            continue
        if error == "slow_down":
            current_interval = min(current_interval + 1, interval_cap)
            sleep(current_interval)
            continue
        raise TokenExchangeFailed(
            redact_sensitive_text(str(payload.get("error_description") or error or "device auth failed"))
        )
    raise TimeoutError("Timed out waiting for device authorization")


def request_device_authorization(
    config: Union["DeviceAuthorizationConfig", OAuthProviderConfig],
    *,
    post_form: Optional[Callable[[str, Dict[str, str]], Dict[str, Any]]] = None,
    timeout: float = 30.0,
    now: Optional[Callable[[], float]] = None,
):
    """POST to the device authorization endpoint. Returns a DeviceAuthorizationSession.

    Never logs or returns secrets beyond the RFC user-facing fields.
    """
    # Local import avoids a hard cycle at module load.
    from .device_models import DeviceAuthorizationConfig, DeviceAuthorizationSession

    if isinstance(config, DeviceAuthorizationConfig):
        endpoint = config.device_authorization_endpoint
        client_id = config.client_id
        scopes = config.scopes
        extra = dict(config.extra_authorization_parameters or {})
        provider_id = config.provider_id
        audience = config.audience
        min_interval = int(config.min_poll_interval_seconds)
        if config.client_secret:
            # Rare for public device clients; supported if provider requires it.
            extra.setdefault("client_secret", config.client_secret)
    else:
        endpoint = config.device_code_url or ""
        client_id = config.client_id
        scopes = config.scopes
        extra = {}
        provider_id = config.provider_id
        audience = None
        min_interval = 5

    if not (endpoint or "").strip():
        raise TokenExchangeFailed("device authorization endpoint is not configured")
    if not (client_id or "").strip():
        raise TokenExchangeFailed("OAuth client_id is not configured")

    data: Dict[str, str] = {"client_id": client_id}
    if scopes:
        data["scope"] = " ".join(scopes)
    if audience:
        data["audience"] = str(audience)
    for key, value in extra.items():
        data[str(key)] = str(value)

    poster = post_form or (lambda url, form: _post_form(url, form, timeout=timeout))
    try:
        payload = poster(endpoint, data)
    except TokenExchangeFailed:
        raise
    except Exception:
        raise TokenExchangeFailed(
            redact_sensitive_text("device authorization request failed")
        ) from None

    if not isinstance(payload, dict):
        raise TokenExchangeFailed("device authorization returned invalid JSON")
    if payload.get("error"):
        raise TokenExchangeFailed(
            redact_sensitive_text(
                str(payload.get("error_description") or payload.get("error") or "device auth failed")
            )
        )

    device_code = payload.get("device_code")
    user_code = payload.get("user_code")
    verification_uri = payload.get("verification_uri") or payload.get("verification_url")
    if not isinstance(device_code, str) or not device_code:
        raise TokenExchangeFailed("device authorization missing device_code")
    if not isinstance(user_code, str) or not user_code:
        raise TokenExchangeFailed("device authorization missing user_code")
    if not isinstance(verification_uri, str) or not verification_uri:
        raise TokenExchangeFailed("device authorization missing verification_uri")

    complete = payload.get("verification_uri_complete") or payload.get("verification_url_complete")
    if complete is not None and not isinstance(complete, str):
        complete = None

    try:
        expires_in = int(payload.get("expires_in") or 900)
    except (TypeError, ValueError):
        expires_in = 900
    try:
        interval = int(payload.get("interval") or min_interval)
    except (TypeError, ValueError):
        interval = min_interval
    interval = max(min_interval, interval)

    wall = (now or time.time)()
    return DeviceAuthorizationSession(
        provider_id=provider_id,
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=complete,
        expires_at=wall + max(1, expires_in),
        interval=float(interval),
    )

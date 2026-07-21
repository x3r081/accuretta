"""Generic OAuth client driven by provider configuration.

Adapted from Nous Research Hermes Agent (MIT) authorization URL assembly,
code exchange, and device-code polling primitives. Accuretta-specific:
configuration objects instead of provider if/else trees; no Hermes client
IDs or provider registrations. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

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


def _post_form(url: str, data: Dict[str, str], *, timeout: float = 30.0) -> Dict[str, Any]:
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
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        raise TokenExchangeFailed(
            redact_sensitive_text(f"token endpoint HTTP {exc.code}: {detail[:200]}")
        ) from None
    except Exception as exc:
        raise TokenExchangeFailed(
            redact_sensitive_text(f"token endpoint request failed: {type(exc).__name__}")
        ) from None

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise TokenExchangeFailed("token endpoint returned non-JSON body") from exc
    if not isinstance(payload, dict):
        raise TokenExchangeFailed("token endpoint returned invalid JSON")
    return payload


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
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        token_type=str(token_type),
        scope=str(scope) if scope is not None else None,
        raw=dict(payload),
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
        raise TokenRefreshFailed(str(exc), provider_id=config.provider_id) from None
    if payload.get("error"):
        raise TokenRefreshFailed(
            redact_sensitive_text(str(payload.get("error_description") or payload.get("error"))),
            provider_id=config.provider_id,
        )
    try:
        return parse_token_response(payload)
    except TokenExchangeFailed as exc:
        raise TokenRefreshFailed(str(exc), provider_id=config.provider_id) from None


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

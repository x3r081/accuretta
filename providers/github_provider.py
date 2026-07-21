"""GitHub account authentication via OAuth device flow.

Newly written for Accuretta. Authenticates the user's GitHub account only.
Does **not** implement GitHub Copilot inference or advertise Copilot access.

Requires Accuretta-owned OAuth App client ID via ACCURETTA_GITHUB_CLIENT_ID.
Never reuses Hermes / VS Code / GitHub CLI / Copilot client IDs.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from auth.device_models import DeviceAuthorizationConfig, DeviceAuthorizationResult
from auth.models import StoredCredential
from auth.redact import redact_sensitive_text
from auth.store import AuthStore

from .base import (
    ApiMode,
    AuthType,
    ProviderCapabilities,
    ProviderDefinition,
)
from .errors import AuthenticationRequired, ProviderUnavailable
from .registry import ProviderRegistry, get_default_registry

log = logging.getLogger("accuretta.providers.github")

GITHUB_PROVIDER_ID = "github"

# Minimal identity scope only.
# read:user — Grants read access to profile data needed to show account label.
# Do NOT request repo, workflow, admin:org, delete:packages, or user:email
# unless a future feature genuinely needs them. Copilot scopes are not requested.
GITHUB_SCOPES = ("read:user",)

GITHUB_DEVICE_ENDPOINT = "https://github.com/login/device/code"
GITHUB_TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"
GITHUB_USER_ENDPOINT = "https://api.github.com/user"

ENV_CLIENT_ID = "ACCURETTA_GITHUB_CLIENT_ID"


def github_client_id() -> str:
    return (os.environ.get(ENV_CLIENT_ID) or "").strip()


def github_disabled_reason() -> Optional[str]:
    if github_client_id():
        return None
    return (
        "Set ACCURETTA_GITHUB_CLIENT_ID to an Accuretta-owned GitHub OAuth App "
        "client ID to enable GitHub account login"
    )


def build_github_definition() -> ProviderDefinition:
    client_id = github_client_id()
    return ProviderDefinition(
        id=GITHUB_PROVIDER_ID,
        display_name="GitHub",
        api_mode=ApiMode.OPENAI_CHAT,  # unused — no inference
        auth_type=AuthType.OAUTH_DEVICE,
        capabilities=ProviderCapabilities(
            streaming=False,
            tools=False,
            vision=False,
            cancellation=True,
            model_listing=False,
            account_authentication=True,
            device_authorization=True,
            inference=False,
        ),
        default_base_url=None,
        supports_model_listing=False,
        supports_inference=False,
        experimental=True,
        enabled=bool(client_id),
        disabled_reason=github_disabled_reason(),
    )


def github_device_config() -> DeviceAuthorizationConfig:
    client_id = github_client_id()
    if not client_id:
        raise ProviderUnavailable(
            github_disabled_reason() or "GitHub client ID not configured",
            provider_id=GITHUB_PROVIDER_ID,
        )
    return DeviceAuthorizationConfig(
        provider_id=GITHUB_PROVIDER_ID,
        client_id=client_id,
        device_authorization_endpoint=GITHUB_DEVICE_ENDPOINT,
        token_endpoint=GITHUB_TOKEN_ENDPOINT,
        scopes=GITHUB_SCOPES,
        min_poll_interval_seconds=5,
        max_poll_interval_seconds=30,
        # GitHub expects Accept: application/json on token responses — handled
        # by auth.oauth_client._post_form Accept header.
    )


def ensure_github_registered(registry: Optional[ProviderRegistry] = None) -> None:
    from auth.device_registry import register_device_config_factory, unregister_device_config_factory

    reg = registry if registry is not None else get_default_registry()
    reg.register(build_github_definition(), factory=None)
    unregister_device_config_factory(GITHUB_PROVIDER_ID)
    if github_client_id():
        register_device_config_factory(GITHUB_PROVIDER_ID, github_device_config)


def validate_github_account(
    access_token: str,
    *,
    transport: Optional[Callable[..., Dict[str, Any]]] = None,
    user_endpoint: str = GITHUB_USER_ENDPOINT,
) -> dict:
    """Lightweight GET /user validation. Returns minimal safe metadata only."""
    if not access_token:
        raise AuthenticationRequired(
            "GitHub access token missing",
            provider_id=GITHUB_PROVIDER_ID,
        )
    if transport is not None:
        payload = transport("GET", user_endpoint, access_token=access_token)
    else:
        req = urllib.request.Request(
            user_endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "Accuretta",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=20.0) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = getattr(resp, "status", 200) or 200
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                exc.read()
            except Exception:
                pass
            if status in (401, 403):
                raise AuthenticationRequired(
                    "GitHub rejected the access token",
                    provider_id=GITHUB_PROVIDER_ID,
                ) from None
            if status == 429:
                raise ProviderUnavailable(
                    "GitHub rate limit exceeded; try again later",
                    provider_id=GITHUB_PROVIDER_ID,
                ) from None
            raise ProviderUnavailable(
                "GitHub account validation failed",
                provider_id=GITHUB_PROVIDER_ID,
            ) from None
        except Exception:
            raise ProviderUnavailable(
                "GitHub account validation failed",
                provider_id=GITHUB_PROVIDER_ID,
            ) from None
        if status != 200:
            raise ProviderUnavailable(
                "GitHub account validation failed",
                provider_id=GITHUB_PROVIDER_ID,
            )
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise ProviderUnavailable(
                "GitHub returned an invalid profile response",
                provider_id=GITHUB_PROVIDER_ID,
            ) from None

    if not isinstance(payload, dict):
        raise ProviderUnavailable(
            "GitHub returned an invalid profile response",
            provider_id=GITHUB_PROVIDER_ID,
        )
    login = payload.get("login")
    account_id = payload.get("id")
    if not isinstance(login, str) or not login.strip():
        raise ProviderUnavailable(
            "GitHub profile missing login",
            provider_id=GITHUB_PROVIDER_ID,
        )
    # Minimal safe metadata — never store the full profile blob.
    out = {
        "account_label": login.strip(),
        "account_id": str(account_id) if account_id is not None else None,
        "validated_at": time.time(),
    }
    return out


def store_github_device_result(
    store: AuthStore,
    result: DeviceAuthorizationResult,
    *,
    validate: Optional[Callable[[str], dict]] = None,
) -> StoredCredential:
    """Validate account then persist minimal credential + metadata."""
    validator = validate or (lambda tok: validate_github_account(tok))
    try:
        meta = validator(result.access_token)
    except AuthenticationRequired:
        raise
    except ProviderUnavailable:
        raise

    scopes = list(result.scopes) if result.scopes else list(GITHUB_SCOPES)
    cred = StoredCredential(
        provider_id=GITHUB_PROVIDER_ID,
        access_token=result.access_token,
        refresh_token=result.refresh_token,  # usually None for GitHub
        expires_at=result.expires_at,  # usually None — do not invent expiry
        token_type=result.token_type or "Bearer",
        scopes=scopes,
        metadata={
            "credential_type": "oauth_device",
            "credential_stored": True,
            "credential_validated": True,
            "account_label": meta.get("account_label"),
            "account_id": meta.get("account_id"),
            "validated_at": meta.get("validated_at"),
            "granted_scopes": scopes,
        },
    )
    store.save(GITHUB_PROVIDER_ID, cred)
    log.info(
        "github account connected label=%s",
        meta.get("account_label"),
    )
    return cred


def sanitize_github_error(exc: BaseException) -> str:
    return redact_sensitive_text(str(exc))[:240]

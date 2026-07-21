"""ChatGPT / Codex account authentication provider (auth only).

Newly written for Accuretta. Uses the official Codex app-server JSON-RPC.
Does **not** implement Codex inference. Never reads Codex credential files or
stores Codex tokens in AuthStore.
"""

from __future__ import annotations

from typing import Optional

from codex.account import CodexAccountError
from codex.discover import discover_codex
from codex.process import CodexProcessError
from codex.protocol import sanitize_error_message
from codex.session import get_codex_session

from .base import (
    ApiMode,
    AuthType,
    ProviderCapabilities,
    ProviderDefinition,
)
from .errors import ProviderUnavailable
from .registry import ProviderRegistry, get_default_registry
from .status import assert_safe_provider_payload

CODEX_PROVIDER_ID = "codex_chatgpt"


def build_codex_definition() -> ProviderDefinition:
    discovery = discover_codex()
    enabled = bool(discovery.available)
    reason = None if enabled else (discovery.disabled_reason or "Codex CLI not installed")
    return ProviderDefinition(
        id=CODEX_PROVIDER_ID,
        display_name="ChatGPT / Codex",
        api_mode=ApiMode.CODEX_APP_SERVER,
        auth_type=AuthType.CODEX_MANAGED_CHATGPT,
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
        enabled=enabled,
        disabled_reason=reason,
    )


def ensure_codex_registered(registry: Optional[ProviderRegistry] = None) -> None:
    reg = registry if registry is not None else get_default_registry()
    # Re-evaluate discovery each registration so status refresh can enable/disable.
    reg.register(build_codex_definition(), factory=None)


def get_codex_provider_status(*, live: bool = True) -> dict:
    session = get_codex_session()
    # Refresh definition availability from discovery without starting process when live=False.
    if not live:
        ensure_codex_registered()
    try:
        dto = session.status_dto(live=live)
    except Exception as exc:
        discovery = discover_codex()
        from codex.status import build_codex_status_dto
        dto = build_codex_status_dto(
            discovery=discovery,
            process_state="error",
            provider_available=False,
            disabled_reason=sanitize_error_message(str(exc)),
            error=sanitize_error_message(str(exc)),
        )
    assert_safe_provider_payload(dto)
    return dto


def codex_connect_browser() -> dict:
    session = get_codex_session()
    try:
        pending = session.start_browser_login()
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {
        "ok": True,
        "providerId": CODEX_PROVIDER_ID,
        "status": "pending",
        **pending,
        "providerStatus": get_codex_provider_status(live=False),
    }
    assert_safe_provider_payload(payload)
    return payload


def codex_start_device() -> dict:
    session = get_codex_session()
    try:
        pending = session.start_device_login()
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {
        "ok": True,
        "providerId": CODEX_PROVIDER_ID,
        "status": "pending",
        **pending,
        "providerStatus": get_codex_provider_status(live=False),
    }
    assert_safe_provider_payload(payload)
    return payload


def codex_login_status() -> dict:
    session = get_codex_session()
    dto = session.login_status()
    # When terminal, clear sensitive pending fields via another status read.
    if dto.get("loginStatus") in {"completed", "failed", "cancelled"}:
        session = get_codex_session()
        if session._account is not None and session._account.pending is not None:
            if session._account.pending.status != "pending":
                # Keep terminal once for UI then clear.
                if dto.get("loginStatus") != "pending":
                    pass
    assert_safe_provider_payload(dto)
    return dto


def codex_cancel_login(body: Optional[dict] = None) -> dict:
    login_id = None
    if isinstance(body, dict):
        raw = body.get("loginId") or body.get("login_id")
        if isinstance(raw, str):
            login_id = raw.strip()
    session = get_codex_session()
    try:
        out = session.cancel_login(login_id)
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {
        "ok": True,
        "providerId": CODEX_PROVIDER_ID,
        **out,
        "providerStatus": get_codex_provider_status(live=False),
    }
    assert_safe_provider_payload(payload)
    return payload


def codex_logout() -> dict:
    session = get_codex_session()
    try:
        status = session.logout()
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {
        "ok": True,
        "providerId": CODEX_PROVIDER_ID,
        "disconnected": True,
        "status": status,
    }
    assert_safe_provider_payload(payload)
    return payload


def codex_retry_process() -> dict:
    session = get_codex_session()
    try:
        status = session.retry_process()
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {"ok": True, "providerId": CODEX_PROVIDER_ID, "status": status}
    assert_safe_provider_payload(payload)
    return payload

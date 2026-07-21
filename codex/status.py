"""Safe frontend DTOs for Codex ChatGPT authentication.

Newly written for Accuretta.
"""

from __future__ import annotations

from typing import Any, Optional

from providers.status import assert_safe_provider_payload

from .discover import CodexDiscovery
from .protocol import PendingLogin, SafeAccountView, sanitize_error_message


def build_codex_status_dto(
    *,
    discovery: CodexDiscovery,
    process_state: str,
    account: Optional[SafeAccountView] = None,
    pending: Optional[PendingLogin] = None,
    provider_available: bool = False,
    disabled_reason: Optional[str] = None,
    error: Optional[str] = None,
    capabilities: Optional[dict] = None,
    inference_flag_enabled: bool = False,
) -> dict:
    acct = account or SafeAccountView()
    reason = disabled_reason or discovery.disabled_reason
    out: dict[str, Any] = {
        "providerId": "codex_chatgpt",
        "displayName": "ChatGPT / Codex",
        "authType": "codex_managed_chatgpt",
        "apiMode": "codex_app_server",
        "experimental": True,
        # UI chat selection stays off until a later milestone wires inference.
        "supportsInference": False,
        "supportsModelListing": False,
        "supportsAccountAuthentication": True,
        "selectable": False,
        # Distinct from auth: backend inference gate (default off).
        "inferenceFlagEnabled": bool(inference_flag_enabled),
        "installed": bool(discovery.executable),
        "available": bool(provider_available),
        "processState": process_state,
        "codexVersion": discovery.version,
        "discoverySource": discovery.source,
        "authenticated": bool(acct.authenticated),
        "authMode": acct.auth_mode,
        "planType": acct.plan_type,
        "accountLabel": acct.account_label,
        "loginStatus": "idle",
        "loginMethod": None,
        "loginId": None,
        "disabledReason": reason if not provider_available else None,
        "error": sanitize_error_message(error) if error else None,
        "capabilities": [
            name
            for name, on in (
                ("account_authentication", True),
                ("browser_login", bool((capabilities or {}).get("browser_login", True))),
                ("device_login", bool((capabilities or {}).get("device_login", True))),
                ("inference_backend", bool(inference_flag_enabled)),
            )
            if on
        ],
    }
    if pending is not None:
        out["loginStatus"] = pending.status
        out["loginMethod"] = pending.method
        out["loginId"] = pending.login_id
        if pending.status == "pending":
            if pending.auth_url:
                out["authUrl"] = pending.auth_url
            if pending.verification_url:
                out["verificationUrl"] = pending.verification_url
            if pending.user_code:
                out["userCode"] = pending.user_code
        if pending.error:
            out["error"] = pending.error
    assert_safe_provider_payload(out)
    return out

"""Structured Codex inference readiness (distinct from account authentication).

Newly written for Accuretta.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Optional, Union

from codex.discover import CodexDiscovery, discover_codex
from codex.flags import is_codex_inference_enabled
from codex.inference import get_codex_inference_service
from codex.protocol import sanitize_error_message
from codex.session import get_codex_session

from .status import assert_safe_provider_payload

# Ops/test override: force protocol-unsupported without changing the CLI.
ENV_PROTOCOL_SUPPORTED = "ACCURETTA_CODEX_PROTOCOL_SUPPORTED"

# Precise, user-facing reasons for Settings (stable copy for UI + tests).
UI_REASON_INFERENCE_DISABLED = "Codex inference disabled in this build"
UI_REASON_NOT_SIGNED_IN = "Sign in with ChatGPT first"
UI_REASON_CLI_MISSING = "Codex CLI unavailable"
UI_REASON_PROCESS = "Codex app-server unavailable"
UI_REASON_PROTOCOL = "Codex protocol unsupported"
UI_REASON_UNAVAILABLE = "Codex unavailable"


class CodexInferenceStatus(str, Enum):
    READY = "ready"
    INFERENCE_DISABLED = "inference_disabled"
    NOT_SIGNED_IN = "not_signed_in"
    CLI_MISSING = "cli_missing"
    PROCESS_UNAVAILABLE = "process_unavailable"
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"
    ERROR = "error"


def ui_selection_reason(status: Optional[Union[str, CodexInferenceStatus]]) -> Optional[str]:
    """Map readiness status to Settings copy (None when selectable)."""
    if status is None:
        return None
    key = status.value if isinstance(status, CodexInferenceStatus) else str(status)
    return {
        CodexInferenceStatus.READY.value: None,
        CodexInferenceStatus.INFERENCE_DISABLED.value: UI_REASON_INFERENCE_DISABLED,
        CodexInferenceStatus.NOT_SIGNED_IN.value: UI_REASON_NOT_SIGNED_IN,
        CodexInferenceStatus.CLI_MISSING.value: UI_REASON_CLI_MISSING,
        CodexInferenceStatus.PROCESS_UNAVAILABLE.value: UI_REASON_PROCESS,
        CodexInferenceStatus.PROTOCOL_UNSUPPORTED.value: UI_REASON_PROTOCOL,
        CodexInferenceStatus.ERROR.value: UI_REASON_UNAVAILABLE,
    }.get(key, UI_REASON_UNAVAILABLE)


def ui_indicator_for_readiness(readiness: dict) -> dict:
    """Small status chip fields — text labels, not color alone."""
    status = readiness.get("status") or CodexInferenceStatus.ERROR.value
    if status == CodexInferenceStatus.READY.value:
        label = "Codex ready"
        kind = "codex_ready"
    elif status == CodexInferenceStatus.NOT_SIGNED_IN.value:
        label = "Codex sign-in required"
        kind = "codex_sign_in_required"
    elif status == CodexInferenceStatus.INFERENCE_DISABLED.value:
        label = "Codex disabled"
        kind = "codex_disabled"
    else:
        label = "Codex unavailable"
        kind = "codex_unavailable"
    return {
        "kind": kind,
        "label": label,
        "detail": ui_selection_reason(status),
    }


def _protocol_supported(discovery: CodexDiscovery) -> bool:
    """Whether the installed Codex speaks the stable thread/turn surface."""
    raw = (os.environ.get(ENV_PROTOCOL_SUPPORTED) or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    # Capability probing preferred over rigid version gates; CLI present ⇒ assume OK.
    return bool(discovery.available and discovery.executable)


def assess_codex_inference_readiness(*, live: bool = True) -> dict:
    """Return a structured readiness DTO (never a single bare boolean)."""
    flag = is_codex_inference_enabled()
    discovery = discover_codex()
    cli_ok = bool(discovery.available and discovery.executable)
    process_ready = False
    authenticated = False
    protocol_ok = _protocol_supported(discovery) if cli_ok else False
    status = CodexInferenceStatus.INFERENCE_DISABLED
    reason: Optional[str] = "Codex inference is disabled (ACCURETTA_CODEX_INFERENCE_ENABLED)"

    if not flag:
        status = CodexInferenceStatus.INFERENCE_DISABLED
        reason = UI_REASON_INFERENCE_DISABLED
    elif not cli_ok:
        status = CodexInferenceStatus.CLI_MISSING
        reason = UI_REASON_CLI_MISSING
    elif not protocol_ok:
        status = CodexInferenceStatus.PROTOCOL_UNSUPPORTED
        reason = UI_REASON_PROTOCOL
    else:
        try:
            svc = get_codex_inference_service()
            avail = svc.check_available(live=live)
            process_ready = bool(avail.process_ready)
            authenticated = bool(avail.authenticated)
            # Process/protocol failures must not be reported as "not signed in"
            # unless account state was actually observed as signed out.
            auth_reason = (avail.reason or "").lower()
            auth_confirmed_signed_out = (
                (not authenticated)
                and process_ready
                and "authentication required" in auth_reason
            )
            auth_unknown = (not authenticated) and (not process_ready) and not auth_confirmed_signed_out
            if auth_confirmed_signed_out:
                status = CodexInferenceStatus.NOT_SIGNED_IN
                reason = UI_REASON_NOT_SIGNED_IN
            elif auth_unknown:
                # Do not instruct the user to sign in while we cannot confirm.
                status = CodexInferenceStatus.PROCESS_UNAVAILABLE if live else CodexInferenceStatus.ERROR
                reason = UI_REASON_PROCESS if live else UI_REASON_UNAVAILABLE
            elif live and not process_ready:
                status = CodexInferenceStatus.PROCESS_UNAVAILABLE
                reason = UI_REASON_PROCESS
            elif not authenticated:
                status = CodexInferenceStatus.NOT_SIGNED_IN
                reason = UI_REASON_NOT_SIGNED_IN
            else:
                status = CodexInferenceStatus.READY
                reason = None
        except Exception as exc:
            status = CodexInferenceStatus.ERROR
            reason = sanitize_error_message(str(exc)) or UI_REASON_UNAVAILABLE
            process_ready = get_codex_session().process_state() == "ready"
            # Preserve last-known auth across unexpected readiness failures.
            try:
                acct = getattr(get_codex_session(), "_account", None)
                if acct is not None and getattr(acct, "account", None) is not None:
                    authenticated = bool(acct.account.authenticated)
            except Exception:
                pass
    indicator = ui_indicator_for_readiness({"status": status.value})
    out = {
        "status": status.value,
        "ready": status == CodexInferenceStatus.READY,
        "flagEnabled": flag,
        "cliAvailable": cli_ok,
        "processReady": process_ready,
        "authenticated": authenticated,
        "protocolSupported": protocol_ok,
        "reason": reason,
        "selectionDisabledReason": ui_selection_reason(status),
        "indicator": indicator,
        # Authentication remains available even when inference is disabled.
        "authenticationIndependent": True,
    }
    assert_safe_provider_payload(out)
    return out


def readiness_to_provider_error(readiness: dict, *, provider_id: str):
    """Map readiness to a raised provider error (no silent fallback)."""
    from .errors import AuthenticationRequired, ProviderUnavailable

    status = readiness.get("status")
    message = (
        readiness.get("selectionDisabledReason")
        or readiness.get("reason")
        or UI_REASON_UNAVAILABLE
    )
    if status == CodexInferenceStatus.NOT_SIGNED_IN.value:
        return AuthenticationRequired(message, provider_id=provider_id)
    return ProviderUnavailable(message, provider_id=provider_id)

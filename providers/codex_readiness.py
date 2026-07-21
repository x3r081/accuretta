"""Structured Codex inference readiness (distinct from account authentication).

Newly written for Accuretta.
"""

from __future__ import annotations

import os
from enum import Enum

from codex.discover import CodexDiscovery, discover_codex
from codex.flags import is_codex_inference_enabled
from codex.inference import get_codex_inference_service
from codex.protocol import sanitize_error_message
from codex.session import get_codex_session

from .status import assert_safe_provider_payload

# Ops/test override: force protocol-unsupported without changing the CLI.
ENV_PROTOCOL_SUPPORTED = "ACCURETTA_CODEX_PROTOCOL_SUPPORTED"


class CodexInferenceStatus(str, Enum):
    READY = "ready"
    INFERENCE_DISABLED = "inference_disabled"
    NOT_SIGNED_IN = "not_signed_in"
    CLI_MISSING = "cli_missing"
    PROCESS_UNAVAILABLE = "process_unavailable"
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"
    ERROR = "error"


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
    reason = "Codex inference is disabled (ACCURETTA_CODEX_INFERENCE_ENABLED)"

    if not flag:
        status = CodexInferenceStatus.INFERENCE_DISABLED
    elif not cli_ok:
        status = CodexInferenceStatus.CLI_MISSING
        reason = discovery.disabled_reason or "Codex CLI not installed"
    elif not protocol_ok:
        status = CodexInferenceStatus.PROTOCOL_UNSUPPORTED
        reason = "Installed Codex version lacks required thread/turn methods"
    else:
        try:
            svc = get_codex_inference_service()
            avail = svc.check_available(live=live)
            process_ready = bool(avail.process_ready)
            authenticated = bool(avail.authenticated)
            if not authenticated:
                status = CodexInferenceStatus.NOT_SIGNED_IN
                reason = "ChatGPT authentication required for Codex inference"
            elif live and not process_ready:
                status = CodexInferenceStatus.PROCESS_UNAVAILABLE
                reason = avail.reason or "Codex app-server is not ready"
            else:
                status = CodexInferenceStatus.READY
                reason = None
        except Exception as exc:
            status = CodexInferenceStatus.ERROR
            reason = sanitize_error_message(str(exc))
            process_ready = get_codex_session().process_state() == "ready"

    out = {
        "status": status.value,
        "ready": status == CodexInferenceStatus.READY,
        "flagEnabled": flag,
        "cliAvailable": cli_ok,
        "processReady": process_ready,
        "authenticated": authenticated,
        "protocolSupported": protocol_ok,
        "reason": reason,
        # Authentication remains available even when inference is disabled.
        "authenticationIndependent": True,
    }
    assert_safe_provider_payload(out)
    return out


def readiness_to_provider_error(readiness: dict, *, provider_id: str):
    """Map readiness to a raised provider error (no silent fallback)."""
    from .errors import AuthenticationRequired, ProviderUnavailable

    status = readiness.get("status")
    message = readiness.get("reason") or "Codex inference is unavailable"
    if status == CodexInferenceStatus.NOT_SIGNED_IN.value:
        return AuthenticationRequired(message, provider_id=provider_id)
    return ProviderUnavailable(message, provider_id=provider_id)

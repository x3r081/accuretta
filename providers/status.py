"""Frontend-safe provider status DTOs.

Newly written for Accuretta. Converts internal provider/auth state into
JSON that may be sent to app.js — never includes tokens or secrets.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional, Set

from .base import ProviderDefinition

# Exact sensitive keys that must never appear in serialized provider payloads.
FORBIDDEN_KEYS = frozenset({
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "code_verifier",
    "authorization",
    "authorization_code",
    "device_code",
    "devicecode",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "apikey",
    "jwt",
    "chatgptauthtokens",
    "raw_credential",
    "raw_response",
    "raw_provider_response",
    "password",
    "api_key",
})

# Case-insensitive whole-value / key patterns that indicate leakage.
# Avoid matching harmless capability substrings (e.g. "authorization" as a word
# in prose is still blocked when it is a key; capability names are curated).
_FORBIDDEN_VALUE_RE = re.compile(
    r"(?i)\b(bearer\s+[a-z0-9._\-]+|access_token|refresh_token|client_secret|code_verifier)\b"
)


def _expires_at_iso(expires_at: Any) -> Optional[str]:
    if expires_at is None:
        return None
    if isinstance(expires_at, str):
        return expires_at
    try:
        epoch = float(expires_at)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_safe_provider_status(
    definition: ProviderDefinition,
    *,
    authenticated: bool = False,
    available: bool = True,
    selected: bool = False,
    is_default: bool = False,
    expires_at: Any = None,
    account_label: Optional[str] = None,
    disabled_reason: Optional[str] = None,
    error: Optional[str] = None,
    credential_stored: Optional[bool] = None,
    credential_validated: Optional[bool] = None,
    model: Optional[str] = None,
    last_validated_at: Any = None,
) -> dict:
    """Safe provider status for HTTP responses / UI."""
    caps = definition.capabilities
    reason = disabled_reason
    if reason is None and not definition.enabled:
        reason = definition.disabled_reason
    out = {
        "providerId": definition.id,
        "displayName": definition.display_name,
        "apiMode": definition.api_mode.value,
        "authType": definition.auth_type.value,
        "authenticated": bool(authenticated),
        "available": bool(available and definition.enabled),
        "selected": bool(selected),
        "isDefault": bool(is_default),
        "experimental": bool(definition.experimental),
        "enabled": bool(definition.enabled),
        "expiresAt": _expires_at_iso(expires_at),
        "accountLabel": account_label,
        "supportsModelListing": bool(definition.supports_model_listing),
        "capabilities": [
            name
            for name, on in (
                ("streaming", caps.streaming),
                ("tools", caps.tools),
                ("vision", caps.vision),
                ("cancellation", caps.cancellation),
                ("model_listing", caps.model_listing),
                ("account_authentication", caps.account_authentication),
                ("device_authorization", caps.device_authorization),
                ("inference", caps.inference),
            )
            if on
        ],
        "supportsInference": bool(getattr(definition, "supports_inference", True)),
        "disabledReason": reason,
        "error": error,
    }
    if credential_stored is not None:
        out["credentialStored"] = bool(credential_stored)
    if credential_validated is not None:
        out["credentialValidated"] = bool(credential_validated)
    if model is not None:
        out["model"] = model
    if last_validated_at is not None:
        out["lastValidatedAt"] = _expires_at_iso(last_validated_at)
    return out


def collect_keys(obj: Any, *, out: Optional[Set[str]] = None) -> Set[str]:
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.add(str(key))
            collect_keys(value, out=out)
    elif isinstance(obj, list):
        for item in obj:
            collect_keys(item, out=out)
    return out


def assert_safe_provider_payload(payload: Any) -> None:
    """Raise ValueError if a provider API payload contains forbidden fields."""
    keys = {k.lower() for k in collect_keys(payload)}
    bad = sorted(keys & {k.lower() for k in FORBIDDEN_KEYS})
    if bad:
        raise ValueError(f"forbidden provider payload keys: {', '.join(bad)}")
    blob = json.dumps(payload, ensure_ascii=False)
    if _FORBIDDEN_VALUE_RE.search(blob):
        raise ValueError("forbidden secret-like value in provider payload")


def sanitize_provider_error_message(message: str, *, limit: int = 240) -> str:
    text = (message or "").strip()
    text = _FORBIDDEN_VALUE_RE.sub("[REDACTED]", text)
    for needle in ("Traceback", "access_token", "refresh_token", "Bearer "):
        if needle.lower() in text.lower():
            text = "Provider error"
            break
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "Provider error"

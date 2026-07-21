"""Secret redaction helpers for logs and diagnostics.

Inspired by Nous Research Hermes Agent (MIT) token fingerprint / redact
patterns. Newly written for Accuretta — do not log raw tokens.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

_SECRET_KEYS = frozenset({
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "authorization",
    "client_secret",
    "code_verifier",
    "device_code",
    "password",
    "api_key",
})

_BEARER_RE = re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]+)", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r"((?:access_token|refresh_token|code|client_secret)=)([^&\s]+)",
    re.IGNORECASE,
)


def token_fingerprint(value: Optional[str], *, length: int = 8) -> str:
    """Short non-reversible fingerprint for safe diagnostics."""
    if not value:
        return "none"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[: max(4, length)]


def mask_secret(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:2]}…{value[-2:]} (fp={token_fingerprint(value)})"


def redact_sensitive_text(text: str) -> str:
    if not text:
        return text
    out = _BEARER_RE.sub(r"\1[REDACTED]", text)
    out = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", out)
    return out


def redact_mapping(data: Any) -> Any:
    """Recursively redact known secret keys from nested structures."""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if str(key).lower() in _SECRET_KEYS:
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_mapping(value)
        return out
    if isinstance(data, list):
        return [redact_mapping(item) for item in data]
    if isinstance(data, str):
        return redact_sensitive_text(data)
    return data

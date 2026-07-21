"""Codex feature flags for Accuretta.

Authentication (ChatGPT login via app-server) is always available when the
Codex CLI is present. Inference is gated separately and is **disabled by
default**.
"""

from __future__ import annotations

import os

ENV_CODEX_INFERENCE_ENABLED = "ACCURETTA_CODEX_INFERENCE_ENABLED"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


def parse_bool_env(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def is_codex_inference_enabled() -> bool:
    """Return True only when ACCURETTA_CODEX_INFERENCE_ENABLED is explicitly on.

    Default is False. Authentication does not depend on this flag.
    """
    return parse_bool_env(ENV_CODEX_INFERENCE_ENABLED, default=False)

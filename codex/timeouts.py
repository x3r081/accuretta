"""Codex turn wall-clock timeout settings (no account / personal data).

Newly written for Accuretta. Approval UI waits use APPROVAL_UI_TIMEOUT_S
separately — this module only covers the overall task/turn deadline.
"""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_CODEX_TURN_TIMEOUT_S = 900
MIN_CODEX_TURN_TIMEOUT_S = 60
MAX_CODEX_TURN_TIMEOUT_S = 3600

# Settings UI presets (seconds).
CODEX_TURN_TIMEOUT_PRESETS = (300, 600, 900, 1800, 3600)


def normalize_codex_turn_timeout_seconds(raw: Any) -> int:
    """Return a safe turn timeout in [60, 3600]; malformed → default 900.

    Out-of-range numeric values are clamped into the accepted range.
    Non-numeric / empty values fall back to DEFAULT_CODEX_TURN_TIMEOUT_S.
    """
    if raw is None or raw == "":
        return DEFAULT_CODEX_TURN_TIMEOUT_S
    try:
        if isinstance(raw, bool):
            return DEFAULT_CODEX_TURN_TIMEOUT_S
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return DEFAULT_CODEX_TURN_TIMEOUT_S
            val = int(float(text))
        elif isinstance(raw, (int, float)):
            val = int(raw)
        else:
            return DEFAULT_CODEX_TURN_TIMEOUT_S
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_CODEX_TURN_TIMEOUT_S
    if val < MIN_CODEX_TURN_TIMEOUT_S:
        return MIN_CODEX_TURN_TIMEOUT_S
    if val > MAX_CODEX_TURN_TIMEOUT_S:
        return MAX_CODEX_TURN_TIMEOUT_S
    return val


def get_codex_turn_timeout_seconds(settings: Optional[dict] = None) -> int:
    if settings is None:
        try:
            import bridge
            settings = bridge.get_settings()
        except Exception:
            return DEFAULT_CODEX_TURN_TIMEOUT_S
    if not isinstance(settings, dict):
        return DEFAULT_CODEX_TURN_TIMEOUT_S
    return normalize_codex_turn_timeout_seconds(settings.get("codex_turn_timeout_seconds"))

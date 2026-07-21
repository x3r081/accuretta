"""Codex idle-watchdog and optional hard-max task duration settings.

Approval UI waits use APPROVAL_UI_TIMEOUT_S separately (codex/approvals.py).

Migration from ``codex_turn_timeout_seconds`` (wall-clock, default 900):
existing installs get ``codex_idle_timeout_seconds = 300`` (5 minutes).
The old 900s total deadline is **not** mapped to a 900s idle timeout.
``codex_turn_timeout_seconds`` is ignored for timeout logic and stripped on save.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

# ---- Idle watchdog (primary) ------------------------------------------------
DEFAULT_CODEX_IDLE_TIMEOUT_S = 300
MIN_CODEX_IDLE_TIMEOUT_S = 60
MAX_CODEX_IDLE_TIMEOUT_S = 1800
CODEX_IDLE_TIMEOUT_PRESETS = (120, 300, 600, 900, 1800)

# ---- Optional hard maximum (safety ceiling; disabled by default) ------------
# None / 0 / "disabled" → no absolute ceiling.
MIN_CODEX_MAX_TASK_DURATION_S = 900
MAX_CODEX_MAX_TASK_DURATION_S = 14400
CODEX_MAX_TASK_DURATION_PRESETS = (900, 1800, 3600, 7200, 14400)

# Deprecated wall-clock key — never drives the idle watchdog.
DEPRECATED_TURN_TIMEOUT_KEY = "codex_turn_timeout_seconds"

# Safe activity categories (no user content).
ACTIVITY_OUTPUT = "output"
ACTIVITY_REASONING = "reasoning"
ACTIVITY_STATUS = "status"
ACTIVITY_TOOL_START = "tool_start"
ACTIVITY_TOOL_PROGRESS = "tool_progress"
ACTIVITY_TOOL_COMPLETE = "tool_complete"
ACTIVITY_FILE_CHANGE = "file_change"
ACTIVITY_APPROVAL_REQUESTED = "approval_requested"
ACTIVITY_APPROVAL_RESOLVED = "approval_resolved"
ACTIVITY_PROTOCOL_PROGRESS = "protocol_progress"

VALID_ACTIVITY_TYPES = frozenset({
    ACTIVITY_OUTPUT,
    ACTIVITY_REASONING,
    ACTIVITY_STATUS,
    ACTIVITY_TOOL_START,
    ACTIVITY_TOOL_PROGRESS,
    ACTIVITY_TOOL_COMPLETE,
    ACTIVITY_FILE_CHANGE,
    ACTIVITY_APPROVAL_REQUESTED,
    ACTIVITY_APPROVAL_RESOLVED,
    ACTIVITY_PROTOCOL_PROGRESS,
})


def normalize_codex_idle_timeout_seconds(raw: Any) -> int:
    """Return idle timeout in [60, 1800]; malformed → 300."""
    if raw is None or raw == "":
        return DEFAULT_CODEX_IDLE_TIMEOUT_S
    try:
        if isinstance(raw, bool):
            return DEFAULT_CODEX_IDLE_TIMEOUT_S
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return DEFAULT_CODEX_IDLE_TIMEOUT_S
            val = int(float(text))
        elif isinstance(raw, (int, float)):
            val = int(raw)
        else:
            return DEFAULT_CODEX_IDLE_TIMEOUT_S
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_CODEX_IDLE_TIMEOUT_S
    if val < MIN_CODEX_IDLE_TIMEOUT_S:
        return MIN_CODEX_IDLE_TIMEOUT_S
    if val > MAX_CODEX_IDLE_TIMEOUT_S:
        return MAX_CODEX_IDLE_TIMEOUT_S
    return val


def normalize_codex_max_task_duration_seconds(raw: Any) -> Optional[int]:
    """Return hard-max seconds, or None when disabled.

    Disabled: None, 0, '', 'disabled', 'off', 'none', false.
    Out-of-range numeric values are clamped into [900, 14400].
    Malformed → None (disabled).
    """
    if raw is None or raw == "" or raw is False:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        text = raw.strip().lower()
        if not text or text in {"0", "disabled", "off", "none", "unlimited", "false"}:
            return None
        try:
            val = int(float(text))
        except (TypeError, ValueError, OverflowError):
            return None
    elif isinstance(raw, (int, float)):
        val = int(raw)
    else:
        return None
    if val <= 0:
        return None
    if val < MIN_CODEX_MAX_TASK_DURATION_S:
        return MIN_CODEX_MAX_TASK_DURATION_S
    if val > MAX_CODEX_MAX_TASK_DURATION_S:
        return MAX_CODEX_MAX_TASK_DURATION_S
    return val


def get_codex_idle_timeout_seconds(settings: Optional[dict] = None) -> int:
    if settings is None:
        try:
            import bridge
            settings = bridge.get_settings()
        except Exception:
            return DEFAULT_CODEX_IDLE_TIMEOUT_S
    if not isinstance(settings, dict):
        return DEFAULT_CODEX_IDLE_TIMEOUT_S
    if "codex_idle_timeout_seconds" in settings:
        return normalize_codex_idle_timeout_seconds(settings.get("codex_idle_timeout_seconds"))
    # Migration: never treat old wall-clock 900 as idle 900 — use default 300.
    return DEFAULT_CODEX_IDLE_TIMEOUT_S


def get_codex_max_task_duration_seconds(settings: Optional[dict] = None) -> Optional[int]:
    if settings is None:
        try:
            import bridge
            settings = bridge.get_settings()
        except Exception:
            return None
    if not isinstance(settings, dict):
        return None
    return normalize_codex_max_task_duration_seconds(
        settings.get("codex_max_task_duration_seconds")
    )


def migrate_codex_timeout_settings(settings: dict) -> Tuple[dict, bool]:
    """Normalize idle/max keys; drop deprecated wall-clock key. Returns (settings, changed)."""
    if not isinstance(settings, dict):
        return {}, False
    out = dict(settings)
    changed = False
    if DEPRECATED_TURN_TIMEOUT_KEY in out:
        out.pop(DEPRECATED_TURN_TIMEOUT_KEY, None)
        changed = True
    idle = normalize_codex_idle_timeout_seconds(out.get("codex_idle_timeout_seconds"))
    if out.get("codex_idle_timeout_seconds") != idle:
        out["codex_idle_timeout_seconds"] = idle
        changed = True
    hard = normalize_codex_max_task_duration_seconds(out.get("codex_max_task_duration_seconds"))
    # Persist disabled as 0 for JSON simplicity.
    persist_hard = 0 if hard is None else hard
    if out.get("codex_max_task_duration_seconds") != persist_hard:
        out["codex_max_task_duration_seconds"] = persist_hard
        changed = True
    return out, changed


# Back-compat shims for older imports (map to idle defaults; do not revive wall-clock).
DEFAULT_CODEX_TURN_TIMEOUT_S = DEFAULT_CODEX_IDLE_TIMEOUT_S
MIN_CODEX_TURN_TIMEOUT_S = MIN_CODEX_IDLE_TIMEOUT_S
MAX_CODEX_TURN_TIMEOUT_S = MAX_CODEX_IDLE_TIMEOUT_S


def normalize_codex_turn_timeout_seconds(raw: Any) -> int:
    """Deprecated: returns idle-timeout normalization (not old wall-clock)."""
    return normalize_codex_idle_timeout_seconds(raw)


def get_codex_turn_timeout_seconds(settings: Optional[dict] = None) -> int:
    """Deprecated alias for get_codex_idle_timeout_seconds."""
    return get_codex_idle_timeout_seconds(settings)

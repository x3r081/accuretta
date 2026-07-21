"""Session-bound inference provider (Accuretta).

Rule: each chat/session binds ``inference_provider_id`` at creation from
Settings. Later Settings changes do not migrate existing sessions. New
messages always dispatch to the session-bound provider — never both, never
silent Codex→local fallback.

Legacy migration (UI + ensure_session_provider):
- Chats without ``inference_provider_id`` are treated as Local llama.cpp
  unless a ``codex_thread_id`` or message ``provider_id`` proves otherwise.
- Missing metadata must never be inferred from the current Settings
  selection (that would flash Codex UI on old local sessions, or the reverse).
- On first touch, unbound legacy chats are persisted as Local (or Codex if
  a thread/message signal exists). Only *new* chats inherit Settings.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from .selection import DEFAULT_PROVIDER_ID, normalize_provider_id

log = logging.getLogger("accuretta.provider.dispatch")

PROVIDER_ID_KEY = "inference_provider_id"
PROVIDER_LABEL_KEY = "inference_provider_label"

_KNOWN = frozenset({"local_llama", "codex_chatgpt", "openai"})


def provider_display_name(provider_id: str) -> str:
    pid = normalize_provider_id(provider_id) or DEFAULT_PROVIDER_ID
    if pid == "codex_chatgpt":
        return "Codex via ChatGPT"
    if pid == "openai":
        return "OpenAI API"
    if pid == DEFAULT_PROVIDER_ID or not pid:
        return "Local llama.cpp"
    return pid


def settings_inference_provider_id(settings: Optional[dict]) -> str:
    """Return Settings' selected provider id without remapping via registry.

    Empty → local. Known and unknown non-empty ids are preserved so the chat
    gate can reject unavailable providers (no silent Codex→local or
    example_cloud→local remap).
    """
    pid = normalize_provider_id((settings or {}).get("provider_id"))
    if not pid:
        return DEFAULT_PROVIDER_ID
    return pid


def mismatch_notice(session_provider_id: str, settings_provider_id: str) -> Optional[str]:
    sid = normalize_provider_id(session_provider_id) or DEFAULT_PROVIDER_ID
    gid = normalize_provider_id(settings_provider_id) or DEFAULT_PROVIDER_ID
    if sid == gid:
        return None
    return (
        f"This session uses {provider_display_name(sid)}. "
        f"New sessions will use {provider_display_name(gid)}."
    )


def _infer_legacy_provider_id(chat: dict) -> Optional[str]:
    """Best-effort bind for chats created before session binding existed."""
    raw_tid = chat.get("codex_thread_id")
    if isinstance(raw_tid, str) and raw_tid.strip():
        return "codex_chatgpt"
    msgs = chat.get("messages") or []
    if not isinstance(msgs, list):
        return None
    for msg in reversed(msgs):
        if not isinstance(msg, dict):
            continue
        pid = normalize_provider_id(msg.get("provider_id"))
        if pid in _KNOWN:
            return pid
    return None


def sanitize_provider_thread_state(chat: dict, provider_id: str) -> None:
    """Never keep a Codex thread on a non-Codex session (or vice-versa seeding)."""
    pid = normalize_provider_id(provider_id) or DEFAULT_PROVIDER_ID
    if pid != "codex_chatgpt":
        if chat.get("codex_thread_id"):
            chat.pop("codex_thread_id", None)
        # cwd is harmless but was only meaningful for Codex turns
        if chat.get("codex_cwd") and pid == DEFAULT_PROVIDER_ID:
            # keep workspace path metadata if present; thread id is the hazard
            pass


def bind_inference_provider(
    chat: dict,
    provider_id: str,
    *,
    label: Optional[str] = None,
) -> str:
    """Write session-bound provider fields. Returns normalized provider id."""
    pid = normalize_provider_id(provider_id) or DEFAULT_PROVIDER_ID
    chat[PROVIDER_ID_KEY] = pid
    chat[PROVIDER_LABEL_KEY] = label or provider_display_name(pid)
    sanitize_provider_thread_state(chat, pid)
    return pid


def ensure_session_provider(
    chat: dict,
    settings: Optional[dict],
    *,
    for_new_chat: bool = False,
) -> Tuple[str, str, Optional[str]]:
    """Ensure ``chat`` has a bound inference provider.

    Returns ``(provider_id, label, mismatch_notice_or_none)``.

    - New chats: inherit Settings provider.
    - Existing bound chats: keep bound provider.
    - Legacy unbound chats: infer from codex_thread_id / message metadata,
      else inherit Settings once and persist on the chat dict.
    """
    settings = settings if isinstance(settings, dict) else {}
    settings_pid = settings_inference_provider_id(settings)

    existing = normalize_provider_id(chat.get(PROVIDER_ID_KEY))
    if existing:
        sanitize_provider_thread_state(chat, existing)
        label = chat.get(PROVIDER_LABEL_KEY) or provider_display_name(existing)
        chat[PROVIDER_LABEL_KEY] = label
        return existing, str(label), mismatch_notice(existing, settings_pid)

    if for_new_chat:
        pid = bind_inference_provider(chat, settings_pid)
        return pid, provider_display_name(pid), None

    # Legacy unbound: infer from thread/message metadata; otherwise Local —
    # never inherit current Settings (would silently migrate old local chats
    # when the user has switched the default provider).
    legacy = _infer_legacy_provider_id(chat)
    pid = bind_inference_provider(chat, legacy or DEFAULT_PROVIDER_ID)
    return pid, provider_display_name(pid), mismatch_notice(pid, settings_pid)


def log_chat_dispatch(
    *,
    session_id: str,
    settings_provider_id: str,
    dispatched_provider_id: str,
    turn_id: Optional[str] = None,
) -> None:
    """Dev-safe structured log — no prompts, tokens, or account payloads.

    Emits via the module logger *and* stderr so operator bridge logs always
    capture dispatch evidence (the root logger is often unconfigured).
    """
    line = (
        "chat_dispatch "
        f"session={(session_id or '')[:64]} "
        f"settings_provider={(settings_provider_id or '')[:64]} "
        f"dispatched={(dispatched_provider_id or '')[:64]} "
        f"turn={(turn_id or '')[:64]}"
    )
    log.info("%s", line)
    try:
        import sys

        print(f"[provider] {line}", file=sys.stderr, flush=True)
    except Exception:
        pass


def safe_session_provider_dto(chat: Optional[dict], settings: Optional[dict] = None) -> dict[str, Any]:
    """Safe fields for API/UI (never secrets)."""
    chat = chat if isinstance(chat, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    pid, label, notice = ensure_session_provider(chat, settings, for_new_chat=False)
    settings_pid = settings_inference_provider_id(settings)
    return {
        "inferenceProviderId": pid,
        "inferenceProviderLabel": label,
        "settingsProviderId": settings_pid,
        "settingsProviderLabel": provider_display_name(settings_pid),
        "providerMismatch": bool(notice),
        "providerMismatchNotice": notice,
    }

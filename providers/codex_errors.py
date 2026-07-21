"""User-facing Codex chat error messages (never raw RPC / tokens).

Newly written for Accuretta.

Prefer explicit ``CodexInferenceEventType`` classification. String matching is
only a defensive fallback for legacy / external errors — and must never map
timeouts to an approval message unless the type is APPROVAL_TIMEOUT.
"""

from __future__ import annotations

from typing import Optional

from codex.inference_types import CodexInferenceError, CodexInferenceEventType
from codex.protocol import sanitize_error_message

from .codex_readiness import (
    UI_REASON_INFERENCE_DISABLED,
    UI_REASON_NOT_SIGNED_IN,
    UI_REASON_PROCESS,
    UI_REASON_PROTOCOL,
    UI_REASON_UNAVAILABLE,
    CodexInferenceStatus,
)

MSG_SIGN_IN_EXPIRED = (
    "Sign-in expired. Sign in with ChatGPT in Settings, then try again."
)
MSG_INFERENCE_DISABLED = UI_REASON_INFERENCE_DISABLED
MSG_UNAVAILABLE = UI_REASON_UNAVAILABLE
MSG_APP_SERVER_EXITED = (
    "Codex app-server exited unexpectedly. Use Retry in Settings, then try again."
)
MSG_TURN_CANCELLED = "The Codex task was cancelled."
MSG_THREAD_INVALID = (
    "This conversation’s Codex thread is no longer valid. "
    "A new thread will be started on the next message — or pick Local llama.cpp."
)
MSG_PROTOCOL_FAILURE = "Codex protocol failure. Check the Codex CLI version and try again."
MSG_UNSUPPORTED_MODEL = (
    "That model is not available for Codex via ChatGPT. "
    "Codex will use its default model on the next try — or pick Local llama.cpp."
)
MSG_IDLE_TIMEOUT = (
    "Codex stopped producing activity and may be stuck. "
    "No progress was received during the configured idle period. Retry the task, "
    "increase the idle timeout, or inspect the Codex logs."
)
MSG_MAX_TASK_DURATION = (
    "Codex reached the configured maximum task duration. "
    "The task was still active, but Accuretta stopped it at the optional safety limit."
)
# Deprecated alias — same copy as idle (old wall-clock wording retired).
MSG_TIMEOUT = MSG_IDLE_TIMEOUT
MSG_APPROVAL_TIMEOUT = (
    "The Codex turn timed out while waiting for approval. "
    "Approve or deny the pending request more quickly, or enable Trust writes "
    "for in-workspace files."
)
MSG_WAITING_APPROVAL = "Codex is waiting for file-write approval."
MSG_APPROVAL_UI = "The approval request could not be displayed."
MSG_APPROVAL_REJECTED = "Codex rejected the approval response."
MSG_BUSY = "A Codex turn is already in progress. Wait for it to finish or press Stop."

_LEGACY_AMBIGUOUS_TURN_TIMEOUT = (
    "codex turn timed out while waiting for approval or a reply"
)
_LEGACY_WALL_CLOCK = "configured turn timeout"
_LEGACY_TASK_TIMEOUT = "configured task timeout"


def is_invalid_thread_message(message: Optional[str]) -> bool:
    text = (message or "").lower()
    return any(
        needle in text
        for needle in (
            "unknown thread",
            "thread not found",
            "invalid thread",
            "no such thread",
            "threadid required",
        )
    )


def is_unsupported_model_message(message: Optional[str]) -> bool:
    text = (message or "").lower()
    if "not supported when using codex" in text:
        return True
    return "model" in text and "not supported" in text and "chatgpt" in text


def _message_for_event_type(event_type: CodexInferenceEventType, msg: str) -> Optional[str]:
    """Type-first mapping. Returns None when the type alone is not decisive."""
    if event_type in {
        CodexInferenceEventType.IDLE_TIMEOUT,
        CodexInferenceEventType.TURN_TIMEOUT,  # deprecated alias
    }:
        return MSG_IDLE_TIMEOUT
    if event_type == CodexInferenceEventType.MAX_TASK_DURATION:
        return MSG_MAX_TASK_DURATION
    if event_type == CodexInferenceEventType.APPROVAL_TIMEOUT:
        return MSG_APPROVAL_TIMEOUT
    if event_type == CodexInferenceEventType.CANCELLED:
        return MSG_TURN_CANCELLED
    if event_type == CodexInferenceEventType.AUTHENTICATION_REQUIRED:
        return MSG_SIGN_IN_EXPIRED
    if event_type in {
        CodexInferenceEventType.PROCESS_ERROR,
        CodexInferenceEventType.PROCESS_EXIT,
    }:
        return MSG_APP_SERVER_EXITED
    if event_type == CodexInferenceEventType.UNAVAILABLE:
        cleaned = sanitize_error_message(msg)
        return cleaned or MSG_UNAVAILABLE
    return None


def _legacy_string_fallback(message: str) -> Optional[str]:
    """Defensive fallback for external / pre-rewrite errors only."""
    low = (message or "").lower().strip()
    if not low:
        return None
    if _LEGACY_AMBIGUOUS_TURN_TIMEOUT in low:
        return MSG_IDLE_TIMEOUT
    if "maximum task duration" in low or "safety limit" in low:
        return MSG_MAX_TASK_DURATION
    if "stopped producing activity" in low or "idle period" in low:
        return MSG_IDLE_TIMEOUT
    if _LEGACY_WALL_CLOCK in low or _LEGACY_TASK_TIMEOUT in low:
        return MSG_IDLE_TIMEOUT
    if low in {"codex turn timed out", "turn timed out", "codex timed out"}:
        return MSG_IDLE_TIMEOUT
    if is_invalid_thread_message(message):
        return MSG_THREAD_INVALID
    if is_unsupported_model_message(message):
        return MSG_UNSUPPORTED_MODEL
    if "already in progress" in low:
        return MSG_BUSY
    if "could not be displayed" in low:
        return MSG_APPROVAL_UI
    if "rejected the approval" in low:
        return MSG_APPROVAL_REJECTED
    if "app-server" in low or ("process" in low and "exit" in low):
        return MSG_APP_SERVER_EXITED
    if "sign in" in low or "authentication" in low:
        return MSG_SIGN_IN_EXPIRED
    if "approval" in low and ("timed out" in low or "timeout" in low):
        if "idle" in low or "did not finish" in low or "maximum task" in low:
            return MSG_IDLE_TIMEOUT
        return MSG_APPROVAL_TIMEOUT
    if "timed out" in low or "timeout" in low:
        return MSG_IDLE_TIMEOUT
    return None


def user_message_for_codex_error(
    exc: BaseException,
    *,
    cancelled: bool = False,
) -> str:
    """Map exceptions / readiness to actionable chat UI copy."""
    if cancelled:
        return MSG_TURN_CANCELLED
    if isinstance(exc, CodexInferenceError):
        msg = exc.message or ""
        typed = _message_for_event_type(exc.event_type, msg)
        if typed is not None:
            if exc.event_type != CodexInferenceEventType.PROTOCOL_ERROR:
                return typed
        if exc.event_type == CodexInferenceEventType.PROTOCOL_ERROR:
            fallback = _legacy_string_fallback(msg)
            if fallback is not None:
                if fallback in {
                    MSG_IDLE_TIMEOUT,
                    MSG_MAX_TASK_DURATION,
                    MSG_APPROVAL_TIMEOUT,
                    MSG_BUSY,
                    MSG_THREAD_INVALID,
                    MSG_UNSUPPORTED_MODEL,
                }:
                    return fallback
            low = msg.lower()
            if "protocol" in low and "version" in low:
                return MSG_PROTOCOL_FAILURE
            if "unsupported" in low or "not supported" in low:
                if is_unsupported_model_message(msg):
                    return MSG_UNSUPPORTED_MODEL
                return MSG_PROTOCOL_FAILURE
            cleaned = sanitize_error_message(msg)
            if cleaned and cleaned.lower() not in {"codex turn timed out", "turn failed"}:
                return cleaned
            return MSG_PROTOCOL_FAILURE
        return sanitize_error_message(msg) or MSG_UNAVAILABLE

    code = getattr(exc, "code", None) or ""
    message = getattr(exc, "message", None) or str(exc)
    if code == "authentication_required":
        return MSG_SIGN_IN_EXPIRED
    if UI_REASON_INFERENCE_DISABLED.lower() in (message or "").lower() or "inference disabled" in (message or "").lower():
        return MSG_INFERENCE_DISABLED
    fallback = _legacy_string_fallback(message)
    if fallback is not None:
        return fallback
    if "protocol" in (message or "").lower() and "version" in (message or "").lower():
        return MSG_PROTOCOL_FAILURE
    return sanitize_error_message(message) or MSG_UNAVAILABLE


def user_message_for_readiness_status(status: Optional[str]) -> str:
    if status == CodexInferenceStatus.NOT_SIGNED_IN.value:
        return MSG_SIGN_IN_EXPIRED
    if status == CodexInferenceStatus.INFERENCE_DISABLED.value:
        return MSG_INFERENCE_DISABLED
    if status == CodexInferenceStatus.PROCESS_UNAVAILABLE.value:
        return MSG_APP_SERVER_EXITED
    if status == CodexInferenceStatus.PROTOCOL_UNSUPPORTED.value:
        return MSG_PROTOCOL_FAILURE
    if status == CodexInferenceStatus.CLI_MISSING.value:
        return "Codex CLI unavailable. Install the Codex CLI, then try again."
    return MSG_UNAVAILABLE

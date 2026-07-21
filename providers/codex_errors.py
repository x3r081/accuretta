"""User-facing Codex chat error messages (never raw RPC / tokens).

Newly written for Accuretta.
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
MSG_TURN_CANCELLED = "Turn cancelled."
MSG_THREAD_INVALID = (
    "This conversation’s Codex thread is no longer valid. "
    "A new thread will be started on the next message — or pick Local llama.cpp."
)
MSG_PROTOCOL_FAILURE = "Codex protocol failure. Check the Codex CLI version and try again."
MSG_UNSUPPORTED_MODEL = (
    "That model is not available for Codex via ChatGPT. "
    "Codex will use its default model on the next try — or pick Local llama.cpp."
)
MSG_TIMEOUT = "Codex timed out waiting for a reply. Try again, or pick Local llama.cpp."
MSG_APPROVAL_TIMEOUT = (
    "The Codex turn timed out while waiting for approval. "
    "Approve or deny the pending request more quickly, or enable Trust writes "
    "for in-workspace files."
)
MSG_WAITING_APPROVAL = "Codex is waiting for file-write approval."
MSG_APPROVAL_UI = "The approval request could not be displayed."
MSG_APPROVAL_REJECTED = "Codex rejected the approval response."
MSG_BUSY = "A Codex turn is already in progress. Wait for it to finish or press Stop."


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
        low = msg.lower()
        if exc.event_type == CodexInferenceEventType.AUTHENTICATION_REQUIRED:
            return MSG_SIGN_IN_EXPIRED
        if exc.event_type == CodexInferenceEventType.PROCESS_ERROR:
            return MSG_APP_SERVER_EXITED
        if is_invalid_thread_message(msg):
            return MSG_THREAD_INVALID
        if is_unsupported_model_message(msg):
            return MSG_UNSUPPORTED_MODEL
        if "waiting for approval" in low or "approval" in low and "timed out" in low:
            return MSG_APPROVAL_TIMEOUT
        if "timed out" in low or "timeout" in low:
            if "approval" in low:
                return MSG_APPROVAL_TIMEOUT
            return MSG_TIMEOUT
        if "already in progress" in low:
            return MSG_BUSY
        if "could not be displayed" in low:
            return MSG_APPROVAL_UI
        if "rejected the approval" in low:
            return MSG_APPROVAL_REJECTED
        # Only blame CLI version when the cause is truly an unknown protocol error.
        if exc.event_type == CodexInferenceEventType.PROTOCOL_ERROR:
            if "protocol" in low and "version" in low:
                return MSG_PROTOCOL_FAILURE
            if "unsupported" in low or "not supported" in low:
                return MSG_PROTOCOL_FAILURE
            # Prefer the sanitized specific message over the generic CLI blame.
            cleaned = sanitize_error_message(msg)
            if cleaned and cleaned.lower() not in {"codex turn timed out", "turn failed"}:
                return cleaned
            return MSG_PROTOCOL_FAILURE
        return sanitize_error_message(msg) or MSG_UNAVAILABLE

    code = getattr(exc, "code", None) or ""
    message = getattr(exc, "message", None) or str(exc)
    low = (message or "").lower()
    if code == "authentication_required" or "sign in" in low or "authentication" in low:
        return MSG_SIGN_IN_EXPIRED
    if UI_REASON_INFERENCE_DISABLED.lower() in low or "inference disabled" in low:
        return MSG_INFERENCE_DISABLED
    if is_invalid_thread_message(message):
        return MSG_THREAD_INVALID
    if is_unsupported_model_message(message):
        return MSG_UNSUPPORTED_MODEL
    if "app-server" in low or ("process" in low and "exit" in low):
        return MSG_APP_SERVER_EXITED
    if "waiting for approval" in low or ("approval" in low and "timed out" in low):
        return MSG_APPROVAL_TIMEOUT
    if "timeout" in low or "timed out" in low:
        return MSG_TIMEOUT
    if "already in progress" in low:
        return MSG_BUSY
    if "protocol" in low and "version" in low:
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

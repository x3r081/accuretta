"""Normalized Codex inference events and results.

Newly written for Accuretta. Never carries tokens or raw app-server payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class CodexInferenceEventType(str, Enum):
    STARTED = "started"
    TEXT_DELTA = "text_delta"
    STATUS = "status"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    AUTHENTICATION_REQUIRED = "authentication_required"
    UNAVAILABLE = "unavailable"
    PROTOCOL_ERROR = "protocol_error"
    PROCESS_ERROR = "process_error"
    # Idle watchdog — no meaningful activity for configured idle period.
    IDLE_TIMEOUT = "idle_timeout"
    # Optional absolute safety ceiling (independent of activity).
    MAX_TASK_DURATION = "max_task_duration"
    APPROVAL_TIMEOUT = "approval_timeout"
    # Alias name used in docs / audits (same wire value as PROCESS_ERROR).
    PROCESS_EXIT = "process_error"
    # Deprecated wall-clock alias → idle timeout wire value.
    TURN_TIMEOUT = "idle_timeout"


@dataclass(frozen=True)
class CodexInferenceEvent:
    type: CodexInferenceEventType
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    text_delta: Optional[str] = None
    message: Optional[str] = None
    status: Optional[str] = None

    def to_safe_dict(self) -> dict:
        out = {"type": self.type.value}
        if self.thread_id:
            out["threadId"] = self.thread_id
        if self.turn_id:
            out["turnId"] = self.turn_id
        if self.text_delta is not None:
            out["textDelta"] = self.text_delta
        if self.message:
            out["message"] = self.message
        if self.status:
            out["status"] = self.status
        return out


@dataclass
class CodexInferenceAvailability:
    flag_enabled: bool
    cli_available: bool
    process_ready: bool
    authenticated: bool
    available: bool
    reason: Optional[str] = None

    def to_safe_dict(self) -> dict:
        return {
            "flagEnabled": self.flag_enabled,
            "cliAvailable": self.cli_available,
            "processReady": self.process_ready,
            "authenticated": self.authenticated,
            "available": self.available,
            "reason": self.reason,
            # Auth remains independent of inference.
            "authenticationIndependent": True,
        }


@dataclass
class CodexTurnResult:
    ok: bool
    thread_id: str
    turn_id: Optional[str] = None
    text: str = ""
    status: str = "failed"  # completed | cancelled | failed
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    events: List[CodexInferenceEvent] = field(default_factory=list)

    def to_safe_dict(self) -> dict:
        return {
            "ok": self.ok,
            "threadId": self.thread_id,
            "turnId": self.turn_id,
            "text": self.text,
            "status": self.status,
            "errorType": self.error_type,
            "errorMessage": self.error_message,
            "events": [e.to_safe_dict() for e in self.events],
        }


class CodexInferenceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        event_type: CodexInferenceEventType = CodexInferenceEventType.PROTOCOL_ERROR,
    ):
        super().__init__(message)
        self.event_type = event_type
        self.message = message

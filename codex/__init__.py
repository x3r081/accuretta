"""Codex app-server client for Accuretta.

Newly written for Accuretta. Talks to the official OpenAI Codex CLI
``app-server`` over stdio JSON-RPC. Accuretta never reads Codex credential
files or handles ChatGPT OAuth tokens directly.
"""

from __future__ import annotations

from .discover import CodexDiscovery, discover_codex, reset_discovery_cache
from .flags import ENV_CODEX_INFERENCE_ENABLED, is_codex_inference_enabled
from .inference import CodexInferenceService, get_codex_inference_service
from .inference_types import (
    CodexInferenceAvailability,
    CodexInferenceError,
    CodexInferenceEvent,
    CodexInferenceEventType,
    CodexTurnResult,
)
from .session import CodexSession, get_codex_session, reset_codex_session, shutdown_codex

__all__ = [
    "ENV_CODEX_INFERENCE_ENABLED",
    "CodexDiscovery",
    "CodexInferenceAvailability",
    "CodexInferenceError",
    "CodexInferenceEvent",
    "CodexInferenceEventType",
    "CodexInferenceService",
    "CodexSession",
    "CodexTurnResult",
    "discover_codex",
    "get_codex_inference_service",
    "get_codex_session",
    "is_codex_inference_enabled",
    "reset_codex_session",
    "reset_discovery_cache",
    "shutdown_codex",
]

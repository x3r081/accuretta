"""ChatGPT / Codex provider — account auth + gated inference.

Newly written for Accuretta. Uses the official Codex app-server JSON-RPC.
Codex owns OAuth credentials. Accuretta never stores ChatGPT tokens.

Inference is behind ACCURETTA_CODEX_INFERENCE_ENABLED and is not shown in the
Settings provider dropdown yet (supportsInference stays false for UI).
"""

from __future__ import annotations

import threading
from typing import Any, Iterator, Optional, Sequence

from codex.account import CodexAccountError
from codex.discover import discover_codex
from codex.flags import is_codex_inference_enabled
from codex.inference import CodexInferenceService, get_codex_inference_service
from codex.inference_types import (
    CodexInferenceError,
    CodexInferenceEventType,
)
from codex.process import CodexProcessError
from codex.protocol import sanitize_error_message
from codex.session import get_codex_session

from .base import (
    ApiMode,
    AuthType,
    InferenceEvent,
    InferenceEventType,
    InferenceRequest,
    ProviderCapabilities,
    ProviderDefinition,
    RuntimeCredentials,
)
from .codex_readiness import (
    CodexInferenceStatus,
    assess_codex_inference_readiness,
    readiness_to_provider_error,
)
from .errors import (
    AuthenticationRequired,
    ProviderUnavailable,
)
from .registry import ProviderRegistry, get_default_registry
from .status import assert_safe_provider_payload

CODEX_PROVIDER_ID = "codex_chatgpt"

# chat_id -> codex thread id (in-memory only; not credentials)
_THREAD_BY_CHAT: dict[str, str] = {}
_THREAD_LOCK = threading.Lock()
# cancellation_id / chat_id -> active inference service for cancel routing
_ACTIVE_TURN: dict[str, CodexInferenceService] = {}
_ACTIVE_LOCK = threading.Lock()


def build_codex_definition() -> ProviderDefinition:
    discovery = discover_codex()
    flag = is_codex_inference_enabled()
    cli_ok = bool(discovery.available)
    # Provider remains registered for account auth even when inference is off.
    enabled = cli_ok
    reason = None if enabled else (discovery.disabled_reason or "Codex CLI not installed")
    return ProviderDefinition(
        id=CODEX_PROVIDER_ID,
        display_name="ChatGPT / Codex",
        api_mode=ApiMode.CODEX_APP_SERVER,
        auth_type=AuthType.CODEX_MANAGED_CHATGPT,
        capabilities=ProviderCapabilities(
            streaming=bool(flag),
            tools=False,
            vision=False,
            cancellation=True,
            model_listing=False,
            account_authentication=True,
            device_authorization=True,
            # Backend capability when flag is on; UI still hides via supports_inference=False.
            inference=bool(flag),
        ),
        default_base_url=None,
        supports_model_listing=False,
        # Keep False so Settings dropdown does not list Codex yet.
        supports_inference=False,
        experimental=True,
        enabled=enabled,
        disabled_reason=reason,
    )


def ensure_codex_registered(registry: Optional[ProviderRegistry] = None) -> None:
    reg = registry if registry is not None else get_default_registry()
    reg.register(build_codex_definition(), factory=lambda: CodexProvider())


def get_codex_provider_status(*, live: bool = True) -> dict:
    session = get_codex_session()
    if not live:
        ensure_codex_registered()
    try:
        dto = session.status_dto(live=live)
    except Exception as exc:
        discovery = discover_codex()
        from codex.status import build_codex_status_dto
        dto = build_codex_status_dto(
            discovery=discovery,
            process_state="error",
            provider_available=False,
            disabled_reason=sanitize_error_message(str(exc)),
            error=sanitize_error_message(str(exc)),
            inference_flag_enabled=is_codex_inference_enabled(),
        )
    readiness = assess_codex_inference_readiness(live=False if not live else live)
    dto["inferenceAvailability"] = readiness
    # UI contract: still not selectable / not listed for chat.
    dto["supportsInference"] = False
    dto["selectable"] = False
    assert_safe_provider_payload(dto)
    return dto


def safe_codex_response_metadata(
    *,
    model_label: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> dict:
    """Safe provider metadata for responses / tests (never tokens)."""
    out = {
        "providerId": CODEX_PROVIDER_ID,
        "providerDisplayName": "ChatGPT / Codex",
    }
    if isinstance(model_label, str) and model_label.strip():
        out["modelLabel"] = model_label.strip()[:120]
    # threadId is internal-only; omit from default UI payloads unless requested.
    if thread_id:
        out["_internalThreadId"] = thread_id
    # Strip internal key before assert for external copies.
    public = {k: v for k, v in out.items() if not k.startswith("_")}
    assert_safe_provider_payload(public)
    return out


def _last_user_text(messages: list) -> str:
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
            if parts:
                return "\n".join(parts)
    return ""


class CodexProvider:
    """InferenceProvider for Codex app-server (capability-gated)."""

    def __init__(self, service: Optional[CodexInferenceService] = None):
        self._service = service

    @property
    def definition(self) -> ProviderDefinition:
        return build_codex_definition()

    def _svc(self) -> CodexInferenceService:
        if self._service is not None:
            return self._service
        return get_codex_inference_service()

    def capabilities(self) -> ProviderCapabilities:
        return self.definition.capabilities

    def validate_configuration(self) -> None:
        readiness = assess_codex_inference_readiness(live=True)
        if not readiness.get("ready"):
            raise readiness_to_provider_error(readiness, provider_id=CODEX_PROVIDER_ID)

    def list_models(self) -> Sequence[dict[str, Any]]:
        return []

    def cancel(self, cancellation_id: str) -> bool:
        if not cancellation_id:
            return False
        with _ACTIVE_LOCK:
            svc = _ACTIVE_TURN.get(cancellation_id)
        if svc is None:
            return False
        return bool(svc.cancel_active_turn())

    def stream_response(
        self,
        request: InferenceRequest,
        credentials: Optional[RuntimeCredentials] = None,
    ) -> Iterator[InferenceEvent]:
        _ = credentials  # Codex owns tokens — never use AuthStore credentials.
        readiness = assess_codex_inference_readiness(live=True)
        if not readiness.get("ready"):
            err = readiness_to_provider_error(readiness, provider_id=CODEX_PROVIDER_ID)
            yield InferenceEvent(event_type=InferenceEventType.ERROR, error=err.message)
            raise err

        text = _last_user_text(request.messages)
        if not text and request.system_prompt:
            text = str(request.system_prompt).strip()
        if not text:
            yield InferenceEvent(
                event_type=InferenceEventType.ERROR,
                error="Empty message for Codex",
            )
            raise ProviderUnavailable("Empty message for Codex", provider_id=CODEX_PROVIDER_ID)

        chat_key = request.cancellation_id or (request.extra or {}).get("chat_id") or ""
        chat_key = str(chat_key) if chat_key else ""
        model = (request.model or "").strip() or None
        svc = self._svc()

        if chat_key:
            with _ACTIVE_LOCK:
                _ACTIVE_TURN[chat_key] = svc

        try:
            with _THREAD_LOCK:
                thread_id = _THREAD_BY_CHAT.get(chat_key) if chat_key else None
            if not thread_id:
                try:
                    thread_id = svc.create_thread(model=model)
                except CodexInferenceError as exc:
                    yield InferenceEvent(
                        event_type=InferenceEventType.ERROR,
                        error=exc.message,
                    )
                    raise self._map_inference_error(exc) from None
                if chat_key:
                    with _THREAD_LOCK:
                        _THREAD_BY_CHAT[chat_key] = thread_id

            # Stream by wrapping run_turn's on_event into InferenceEvents via a queue.
            import queue

            q: queue.Queue = queue.Queue()

            def on_event(evt):
                q.put(evt)

            result_box: dict = {}
            error_box: dict = {}

            def _worker():
                try:
                    result_box["r"] = svc.run_turn(
                        thread_id, text, on_event=on_event, timeout_s=300.0
                    )
                except Exception as exc:
                    error_box["e"] = exc
                finally:
                    q.put(None)

            t = threading.Thread(target=_worker, name="codex-turn", daemon=True)
            t.start()
            while True:
                item = q.get()
                if item is None:
                    break
                if item.type == CodexInferenceEventType.TEXT_DELTA and item.text_delta:
                    yield InferenceEvent(
                        event_type=InferenceEventType.TEXT_DELTA,
                        text_delta=item.text_delta,
                    )
                elif item.type == CodexInferenceEventType.STARTED:
                    yield InferenceEvent(
                        event_type=InferenceEventType.TEXT_DELTA,
                        text_delta="",
                        raw={"status": "started", "turnId": item.turn_id},
                    )
                elif item.type in {
                    CodexInferenceEventType.PROTOCOL_ERROR,
                    CodexInferenceEventType.PROCESS_ERROR,
                    CodexInferenceEventType.AUTHENTICATION_REQUIRED,
                    CodexInferenceEventType.UNAVAILABLE,
                }:
                    yield InferenceEvent(
                        event_type=InferenceEventType.ERROR,
                        error=item.message or item.type.value,
                    )
            t.join(timeout=1.0)
            if "e" in error_box:
                exc = error_box["e"]
                if isinstance(exc, CodexInferenceError):
                    yield InferenceEvent(
                        event_type=InferenceEventType.ERROR,
                        error=exc.message,
                    )
                    raise self._map_inference_error(exc) from None
                raise
            result = result_box.get("r")
            if result is None:
                yield InferenceEvent(
                    event_type=InferenceEventType.ERROR,
                    error="Codex turn produced no result",
                )
                raise ProviderUnavailable(
                    "Codex turn produced no result",
                    provider_id=CODEX_PROVIDER_ID,
                )
            if result.status == "cancelled":
                yield InferenceEvent(
                    event_type=InferenceEventType.COMPLETED,
                    completion_reason="cancelled",
                )
                return
            if not result.ok:
                msg = result.error_message or "Codex turn failed"
                yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)
                raise ProviderUnavailable(msg, provider_id=CODEX_PROVIDER_ID)
            yield InferenceEvent(
                event_type=InferenceEventType.COMPLETED,
                completion_reason="stop",
                raw=safe_codex_response_metadata(model_label=model, thread_id=None),
            )
        finally:
            if chat_key:
                with _ACTIVE_LOCK:
                    _ACTIVE_TURN.pop(chat_key, None)

    @staticmethod
    def _map_inference_error(exc: CodexInferenceError):
        if exc.event_type == CodexInferenceEventType.AUTHENTICATION_REQUIRED:
            return AuthenticationRequired(exc.message, provider_id=CODEX_PROVIDER_ID)
        return ProviderUnavailable(exc.message, provider_id=CODEX_PROVIDER_ID)


def cancel_codex_for_chat(chat_id: str) -> bool:
    """Route Stop / cancel to the active Codex turn for this chat, if any."""
    if not chat_id:
        return False
    with _ACTIVE_LOCK:
        svc = _ACTIVE_TURN.get(chat_id)
    if svc is None:
        return False
    return bool(svc.cancel_active_turn())


def clear_codex_thread_for_chat(chat_id: str) -> None:
    with _THREAD_LOCK:
        _THREAD_BY_CHAT.pop(chat_id, None)


# ---- account auth HTTP helpers (unchanged surface) ------------------------

def codex_connect_browser() -> dict:
    session = get_codex_session()
    try:
        pending = session.start_browser_login()
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {
        "ok": True,
        "providerId": CODEX_PROVIDER_ID,
        "status": "pending",
        **pending,
        "providerStatus": get_codex_provider_status(live=False),
    }
    assert_safe_provider_payload(payload)
    return payload


def codex_start_device() -> dict:
    session = get_codex_session()
    try:
        pending = session.start_device_login()
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {
        "ok": True,
        "providerId": CODEX_PROVIDER_ID,
        "status": "pending",
        **pending,
        "providerStatus": get_codex_provider_status(live=False),
    }
    assert_safe_provider_payload(payload)
    return payload


def codex_login_status() -> dict:
    session = get_codex_session()
    dto = session.login_status()
    assert_safe_provider_payload(dto)
    return dto


def codex_cancel_login(body: Optional[dict] = None) -> dict:
    login_id = None
    if isinstance(body, dict):
        raw = body.get("loginId") or body.get("login_id")
        if isinstance(raw, str):
            login_id = raw.strip()
    session = get_codex_session()
    try:
        out = session.cancel_login(login_id)
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {
        "ok": True,
        "providerId": CODEX_PROVIDER_ID,
        **out,
        "providerStatus": get_codex_provider_status(live=False),
    }
    assert_safe_provider_payload(payload)
    return payload


def codex_logout() -> dict:
    session = get_codex_session()
    try:
        status = session.logout()
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {
        "ok": True,
        "providerId": CODEX_PROVIDER_ID,
        "disconnected": True,
        "status": status,
    }
    assert_safe_provider_payload(payload)
    return payload


def codex_retry_process() -> dict:
    session = get_codex_session()
    try:
        status = session.retry_process()
    except (CodexProcessError, CodexAccountError) as exc:
        raise ProviderUnavailable(str(exc), provider_id=CODEX_PROVIDER_ID) from None
    payload = {"ok": True, "providerId": CODEX_PROVIDER_ID, "status": status}
    assert_safe_provider_payload(payload)
    return payload

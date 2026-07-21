"""ChatGPT / Codex provider — account auth + gated inference.

Newly written for Accuretta. Uses the official Codex app-server JSON-RPC.
Codex owns OAuth credentials. Accuretta never stores ChatGPT tokens.

Inference is behind ACCURETTA_CODEX_INFERENCE_ENABLED. The Settings dropdown
lists Codex when supportsInference is true; selection requires readiness.
"""

from __future__ import annotations

import re
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
from .codex_errors import (
    MSG_THREAD_INVALID,
    is_invalid_thread_message,
    user_message_for_codex_error,
)
from .codex_readiness import (
    assess_codex_inference_readiness,
    readiness_to_provider_error,
)
from .codex_workspace import (
    bind_codex_cwd,
    clear_codex_cwd_for_chat,
    resolve_codex_workspace,
    workspace_status_for_ui,
)
from .errors import (
    AuthenticationRequired,
    ProviderUnavailable,
)
from .registry import ProviderRegistry, get_default_registry
from .status import assert_safe_provider_payload

CODEX_PROVIDER_ID = "codex_chatgpt"
CODEX_DISPLAY_NAME = "Codex via ChatGPT"
LOCAL_DISPLAY_NAME = "Local llama.cpp"
_THREAD_BY_CHAT: dict[str, str] = {}
_THREAD_LOCK = threading.Lock()
# cancellation_id / chat_id -> active inference service for cancel routing
_ACTIVE_TURN: dict[str, CodexInferenceService] = {}
_ACTIVE_LOCK = threading.Lock()
# Last completed turn metadata per chat (safe fields only) for persistence.
_LAST_TURN_META: dict[str, dict] = {}
_META_LOCK = threading.Lock()

# Accuretta/local placeholders that must never be sent as Codex model ids.
_CODEX_MODEL_BLOCKLIST = frozenset({
    "codex",
    "local",
    "local_llama",
    "llama",
    "llama.cpp",
    "llamacpp",
})

# llama.cpp quantized GGUF basename suffixes (e.g. qwen…-q4_k_m).
_LOCAL_GGUF_QUANT_RE = re.compile(
    r"(?:^|[-_])q[2-8](?:[_-][0-9a-z_]+)?$",
    re.IGNORECASE,
)


def _looks_like_local_gguf_model_id(name: str) -> bool:
    """Heuristic: local GGUF basenames / paths must not go to Codex."""
    if not name:
        return False
    if name.endswith(".gguf") or name.endswith(".GGUF"):
        return True
    if "/" in name or "\\" in name:
        return True
    return bool(_LOCAL_GGUF_QUANT_RE.search(name.strip()))


def resolve_codex_inference_model(
    settings: Optional[dict] = None,
    *,
    requested: Optional[str] = None,
) -> Optional[str]:
    """Return an explicit Codex model override, or None for Codex defaults.

    Never forwards the local llama.cpp ``settings.model`` id — ChatGPT Codex
    rejects those with an unsupported-model protocol error.
    """
    cand = ""
    if isinstance(requested, str) and requested.strip():
        cand = requested.strip()
    elif isinstance(settings, dict):
        raw = settings.get("codex_model")
        if isinstance(raw, str):
            cand = raw.strip()
    if not cand:
        return None
    if cand.lower() in _CODEX_MODEL_BLOCKLIST:
        return None
    if _looks_like_local_gguf_model_id(cand):
        return None
    return cand


def bind_codex_thread(chat_id: str, thread_id: str) -> None:
    """Remember Accuretta chat → Codex thread mapping (non-secret id only)."""
    if not chat_id or not thread_id:
        return
    tid = str(thread_id).strip()
    if not tid or len(tid) > 200:
        return
    with _THREAD_LOCK:
        _THREAD_BY_CHAT[str(chat_id)] = tid


def get_bound_codex_thread(chat_id: str) -> Optional[str]:
    if not chat_id:
        return None
    with _THREAD_LOCK:
        return _THREAD_BY_CHAT.get(str(chat_id))


def clear_codex_thread_for_chat(chat_id: str) -> None:
    if not chat_id:
        return
    with _THREAD_LOCK:
        _THREAD_BY_CHAT.pop(str(chat_id), None)
    with _META_LOCK:
        _LAST_TURN_META.pop(str(chat_id), None)
    clear_codex_cwd_for_chat(chat_id)


def pop_codex_turn_meta(chat_id: str) -> Optional[dict]:
    if not chat_id:
        return None
    with _META_LOCK:
        return _LAST_TURN_META.pop(str(chat_id), None)


def peek_codex_turn_meta(chat_id: str) -> Optional[dict]:
    if not chat_id:
        return None
    with _META_LOCK:
        meta = _LAST_TURN_META.get(str(chat_id))
        return dict(meta) if isinstance(meta, dict) else None


def _store_turn_meta(chat_id: str, meta: dict) -> None:
    if not chat_id:
        return
    public = {k: v for k, v in meta.items() if not str(k).startswith("_")}
    assert_safe_provider_payload(public)
    with _META_LOCK:
        _LAST_TURN_META[str(chat_id)] = dict(meta)


def build_codex_definition() -> ProviderDefinition:
    discovery = discover_codex()
    flag = is_codex_inference_enabled()
    cli_ok = bool(discovery.available)
    # Provider remains registered for account auth even when inference is off.
    enabled = cli_ok
    reason = None if enabled else (discovery.disabled_reason or "Codex CLI not installed")
    return ProviderDefinition(
        id=CODEX_PROVIDER_ID,
        display_name="Codex via ChatGPT",
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
            inference=bool(flag),
        ),
        default_base_url=None,
        supports_model_listing=False,
        # Listed in Settings; option enabled only when readiness.ready.
        supports_inference=True,
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
    readiness = assess_codex_inference_readiness(live=live)
    dto["inferenceAvailability"] = readiness
    dto["supportsInference"] = True
    dto["displayName"] = "Codex via ChatGPT"
    ready = bool(readiness.get("ready"))
    dto["selectable"] = ready
    # Keep auth availability distinct from inference selection readiness.
    dto["inferenceReady"] = ready
    dto["selectionDisabledReason"] = readiness.get("selectionDisabledReason")
    dto["indicator"] = readiness.get("indicator")
    if not ready and readiness.get("selectionDisabledReason"):
        # Prefer precise selection copy when the option is shown disabled.
        dto["disabledReason"] = readiness.get("selectionDisabledReason")
    dto["workspace"] = workspace_status_for_ui()
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
        "providerDisplayName": CODEX_DISPLAY_NAME,
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
            msg = user_message_for_codex_error(err)
            yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)
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
        model = resolve_codex_inference_model(requested=request.model)
        correlation = str((request.extra or {}).get("correlation_id") or chat_key or "")
        svc = self._svc()

        # Register active turn early so duplicate submissions are rejected
        # before workspace resolution / thread create work.
        if chat_key:
            with _ACTIVE_LOCK:
                if chat_key in _ACTIVE_TURN:
                    busy = ProviderUnavailable(
                        "A Codex turn is already in progress",
                        provider_id=CODEX_PROVIDER_ID,
                    )
                    msg = user_message_for_codex_error(busy)
                    yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)
                    raise busy
                _ACTIVE_TURN[chat_key] = svc

        preferred_cwd = (request.extra or {}).get("codex_cwd") or (request.extra or {}).get("cwd")
        if not isinstance(preferred_cwd, str):
            preferred_cwd = None
        workspace = resolve_codex_workspace(chat_id=chat_key or None, preferred_cwd=preferred_cwd)
        cwd = workspace.cwd if workspace.ok else None
        if cwd and chat_key:
            bind_codex_cwd(chat_key, cwd)

        try:
            thread_id = None
            if chat_key:
                thread_id = get_bound_codex_thread(chat_key)
            extra_tid = (request.extra or {}).get("thread_id") or (request.extra or {}).get(
                "codex_thread_id"
            )
            if not thread_id and isinstance(extra_tid, str) and extra_tid.strip():
                thread_id = extra_tid.strip()
                if chat_key:
                    bind_codex_thread(chat_key, thread_id)

            created_fresh = False

            def _create_thread() -> str:
                return svc.create_thread(model=model, cwd=cwd)

            if not thread_id:
                try:
                    thread_id = _create_thread()
                    created_fresh = True
                except CodexInferenceError as exc:
                    msg = user_message_for_codex_error(exc)
                    yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)
                    raise self._map_inference_error(exc, message=msg) from None
                if chat_key:
                    bind_codex_thread(chat_key, thread_id)

            import queue

            def _map_item(item, *, turn_id_box: dict):
                """Yield provider InferenceEvents for one Codex inference event."""
                if item.type == CodexInferenceEventType.STARTED:
                    turn_id_box["id"] = item.turn_id
                    yield InferenceEvent(
                        event_type=InferenceEventType.TEXT_DELTA,
                        text_delta="",
                        raw={
                            "status": "started",
                            "turnId": item.turn_id,
                            "threadId": thread_id,
                            "correlationId": correlation,
                        },
                    )
                elif item.type == CodexInferenceEventType.TEXT_DELTA and item.text_delta:
                    yield InferenceEvent(
                        event_type=InferenceEventType.TEXT_DELTA,
                        text_delta=item.text_delta,
                        raw={
                            "turnId": item.turn_id or turn_id_box.get("id"),
                            "correlationId": correlation,
                        },
                    )
                elif item.type in {
                    CodexInferenceEventType.PROTOCOL_ERROR,
                    CodexInferenceEventType.PROCESS_ERROR,
                    CodexInferenceEventType.AUTHENTICATION_REQUIRED,
                    CodexInferenceEventType.UNAVAILABLE,
                }:
                    msg = user_message_for_codex_error(
                        CodexInferenceError(
                            item.message or item.type.value,
                            event_type=item.type,
                        )
                    )
                    yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)

            def _run_once(active_thread: str):
                """Run one Codex turn, yielding events live (not buffered until end)."""
                q: queue.Queue = queue.Queue()
                result_box: dict = {}
                error_box: dict = {}

                def on_event(evt):
                    q.put(evt)

                def _worker():
                    try:
                        result_box["r"] = svc.run_turn(
                            active_thread, text, on_event=on_event, timeout_s=300.0
                        )
                    except Exception as exc:
                        error_box["e"] = exc
                    finally:
                        q.put(None)

                t = threading.Thread(target=_worker, name="codex-turn", daemon=True)
                t.start()
                events: list = []
                turn_id_box: dict = {}
                while True:
                    item = q.get()
                    if item is None:
                        break
                    events.append(item)
                    for mapped in _map_item(item, turn_id_box=turn_id_box):
                        yield mapped
                t.join(timeout=1.0)
                yield ("__done__", events, result_box.get("r"), error_box.get("e"), turn_id_box.get("id"))

            # First attempt — stream live. Invalid-thread recovery re-runs once.
            done = None
            for item in _run_once(thread_id):
                if isinstance(item, tuple) and item and item[0] == "__done__":
                    done = item
                    continue
                yield item
            assert done is not None
            _, events, result, err, turn_id = done

            # Invalid / missing remote thread → clear mapping, create once, retry.
            if err is not None and isinstance(err, CodexInferenceError):
                if is_invalid_thread_message(err.message) and chat_key:
                    clear_codex_thread_for_chat(chat_key)
                    try:
                        thread_id = svc.create_thread(model=model, cwd=cwd)
                        created_fresh = True
                        bind_codex_thread(chat_key, thread_id)
                        done = None
                        for item in _run_once(thread_id):
                            if isinstance(item, tuple) and item and item[0] == "__done__":
                                done = item
                                continue
                            yield item
                        assert done is not None
                        _, events, result, err, turn_id = done
                    except CodexInferenceError as exc2:
                        msg = user_message_for_codex_error(exc2)
                        yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)
                        raise self._map_inference_error(exc2, message=msg) from None
                    if err is not None:
                        # Recovery failed — surface thread-invalid guidance.
                        msg = MSG_THREAD_INVALID
                        if isinstance(err, CodexInferenceError):
                            msg = user_message_for_codex_error(err)
                        yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)
                        if isinstance(err, CodexInferenceError):
                            raise self._map_inference_error(err, message=msg) from None
                        raise err

            if err is not None:
                if isinstance(err, CodexInferenceError):
                    msg = user_message_for_codex_error(err)
                    yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)
                    raise self._map_inference_error(err, message=msg) from None
                raise err

            if result is None:
                msg = "Codex turn produced no result"
                yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)
                raise ProviderUnavailable(msg, provider_id=CODEX_PROVIDER_ID)

            meta = {
                "providerId": CODEX_PROVIDER_ID,
                "providerDisplayName": CODEX_DISPLAY_NAME,
                "threadId": thread_id,
                "turnId": result.turn_id or turn_id,
                "correlationId": correlation,
                "createdThread": created_fresh,
                "cwd": cwd,
                "workspaceMode": workspace.mode,
                "codingActionsAllowed": False,
            }
            if model:
                meta["modelLabel"] = str(model)[:120]
            if chat_key:
                _store_turn_meta(chat_key, meta)

            if result.status == "cancelled":
                yield InferenceEvent(
                    event_type=InferenceEventType.COMPLETED,
                    completion_reason="cancelled",
                    raw=meta,
                )
                return
            if not result.ok:
                msg = user_message_for_codex_error(
                    CodexInferenceError(
                        result.error_message or "Codex turn failed",
                        event_type=CodexInferenceEventType.PROTOCOL_ERROR,
                    )
                )
                yield InferenceEvent(event_type=InferenceEventType.ERROR, error=msg)
                raise ProviderUnavailable(msg, provider_id=CODEX_PROVIDER_ID)
            yield InferenceEvent(
                event_type=InferenceEventType.COMPLETED,
                completion_reason="stop",
                raw=meta,
            )
        finally:
            if chat_key:
                with _ACTIVE_LOCK:
                    _ACTIVE_TURN.pop(chat_key, None)

    @staticmethod
    def _map_inference_error(exc: CodexInferenceError, *, message: Optional[str] = None):
        msg = message or user_message_for_codex_error(exc)
        if exc.event_type == CodexInferenceEventType.AUTHENTICATION_REQUIRED:
            return AuthenticationRequired(msg, provider_id=CODEX_PROVIDER_ID)
        return ProviderUnavailable(msg, provider_id=CODEX_PROVIDER_ID)


def cancel_codex_for_chat(chat_id: str) -> bool:
    """Route Stop / cancel to the active Codex turn for this chat, if any."""
    if not chat_id:
        return False
    with _ACTIVE_LOCK:
        svc = _ACTIVE_TURN.get(chat_id)
    if svc is None:
        return False
    return bool(svc.cancel_active_turn())


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

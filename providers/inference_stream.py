"""Narrow chat-completion stream adapter for the agent loop.

Newly written for Accuretta. Lets ``run_chat_turn`` keep orchestration while
LocalLlamaProvider and OpenAIProvider supply OpenAI-compatible SSE bytes.
Codex is adapted into the same SSE framing so the turn loop stays shared,
without falling back to local llama on Codex failure.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Optional, Tuple

from .codex_provider import CODEX_PROVIDER_ID, CodexProvider, cancel_codex_for_chat
from .codex_readiness import assess_codex_inference_readiness
from .errors import AuthenticationRequired, ProviderUnavailable
from .openai_provider import OPENAI_PROVIDER_ID, OpenAIProvider, openai_api_key_from_store
from .selection import DEFAULT_PROVIDER_ID


def build_provider_chat_payload(
    *,
    provider_id: str,
    model: str,
    messages: list,
    settings: dict,
    use_tools: bool,
    native_tools: bool,
    tools_payload: Optional[list],
    llama_options: dict,
    stop: Optional[list] = None,
    chat_template_kwargs: Optional[dict] = None,
) -> dict:
    """Build a chat.completions payload appropriate for the selected provider."""
    payload: dict = {
        "model": model or "local",
        "messages": messages,
        "stream": True,
    }
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if key in llama_options:
            payload[key] = llama_options[key]
    max_tokens = llama_options.get("max_tokens")
    if max_tokens is None:
        try:
            np = int(settings.get("num_predict") or 0)
        except Exception:
            np = 0
        if np > 0:
            max_tokens = np
    if max_tokens is not None and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)

    if provider_id == CODEX_PROVIDER_ID:
        # Codex does not use Accuretta's OpenAI tools payload.
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        return payload

    if use_tools and native_tools and tools_payload:
        payload["tools"] = tools_payload
        payload["tool_choice"] = "auto"

    if provider_id == DEFAULT_PROVIDER_ID:
        for key, value in llama_options.items():
            payload.setdefault(key, value)
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs
        if stop:
            payload["stop"] = list(stop)
    else:
        for k in list(payload.keys()):
            if k in {"chat_template_kwargs", "mirostat", "mirostat_tau", "mirostat_eta"}:
                payload.pop(k, None)
    return payload


class _CodexStreamHandle:
    """Minimal response stand-in so cancel_chat can still call close()."""

    def __init__(self, chat_id: Optional[str] = None):
        self.chat_id = chat_id
        self._closed = False

    def close(self) -> None:
        self._closed = True
        if self.chat_id:
            cancel_codex_for_chat(self.chat_id)

    def read(self, _n: int = 0) -> bytes:
        return b""


def _inference_events_to_openai_sse(events: Iterator) -> Iterator[bytes]:
    from .base import InferenceEventType

    for evt in events:
        if evt.event_type == InferenceEventType.TEXT_DELTA and evt.text_delta:
            chunk = {
                "choices": [{"index": 0, "delta": {"content": evt.text_delta}, "finish_reason": None}],
            }
            yield ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
        elif evt.event_type == InferenceEventType.STATUS:
            note = (evt.raw or {}).get("note") if isinstance(evt.raw, dict) else None
            note = note or evt.error or "Waiting…"
            chunk = {
                "accuretta_notice": note,
                "accuretta_status": (evt.raw or {}).get("status") if isinstance(evt.raw, dict) else None,
                "choices": [],
            }
            yield ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
        elif evt.event_type == InferenceEventType.ERROR:
            err = {"error": {"message": evt.error or "Codex error"}, "choices": []}
            yield ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode("utf-8")
        elif evt.event_type == InferenceEventType.COMPLETED:
            chunk = {
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": evt.completion_reason or "stop",
                }],
            }
            yield ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
    yield b"data: [DONE]\n\n"


def open_provider_chat_stream(
    *,
    provider_id: str,
    payload: dict,
    cancel_ev,
    auth_store=None,
    bridge_module=None,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> Tuple[Any, Iterator[bytes]]:
    """Open a streaming completion for exactly one provider.

    Never falls back from Codex to local llama (or vice versa).
    """
    if provider_id == DEFAULT_PROVIDER_ID or not provider_id:
        bridge = bridge_module
        if bridge is None:
            import bridge as bridge
        resp = bridge.llama_post_stream("/v1/chat/completions", payload)

        def _iter():
            yield from bridge.iter_llama_sse(
                resp,
                idle_timeout=bridge._llama_gen_idle_timeout_s(),
                wall_timeout=bridge._llama_gen_wall_timeout_s(),
                cancel_ev=cancel_ev,
                proc_alive=_local_proc_alive(bridge),
            )

        return resp, _iter()

    if provider_id == OPENAI_PROVIDER_ID:
        if auth_store is None:
            raise AuthenticationRequired(
                "OpenAI API key required",
                provider_id=OPENAI_PROVIDER_ID,
            )
        api_key = openai_api_key_from_store(auth_store)
        if not api_key:
            raise AuthenticationRequired(
                "OpenAI API key required",
                provider_id=OPENAI_PROVIDER_ID,
            )
        provider = OpenAIProvider()
        resp = provider.open_chat_stream(payload, api_key=api_key, cancel_ev=cancel_ev)

        def _iter_openai():
            try:
                while True:
                    if cancel_ev is not None and cancel_ev.is_set():
                        return
                    try:
                        chunk = resp.read(1024)
                    except Exception:
                        return
                    if not chunk:
                        return
                    yield chunk
            finally:
                pass

        return resp, _iter_openai()

    if provider_id == CODEX_PROVIDER_ID:
        from .codex_errors import user_message_for_readiness_status
        from .codex_provider import bind_codex_thread

        readiness = assess_codex_inference_readiness(live=True)
        if not readiness.get("ready"):
            from .errors import AuthenticationRequired, ProviderUnavailable
            detail = user_message_for_readiness_status(readiness.get("status"))
            status = readiness.get("status")
            if status == "not_signed_in":
                raise AuthenticationRequired(detail, provider_id=CODEX_PROVIDER_ID)
            raise ProviderUnavailable(detail, provider_id=CODEX_PROVIDER_ID)

        from .base import InferenceRequest

        if chat_id and thread_id:
            bind_codex_thread(chat_id, thread_id)

        extra = {"chat_id": chat_id} if chat_id else {}
        if thread_id:
            extra["thread_id"] = thread_id
        if correlation_id:
            extra["correlation_id"] = correlation_id
        elif chat_id:
            extra["correlation_id"] = chat_id
        if cwd:
            extra["codex_cwd"] = cwd

        provider = CodexProvider()
        request = InferenceRequest(
            model=str(payload.get("model") or ""),
            messages=list(payload.get("messages") or []),
            cancellation_id=chat_id or "",
            extra=extra,
        )
        handle = _CodexStreamHandle(chat_id=chat_id)

        def _iter_codex():
            try:
                events = provider.stream_response(request)
                for chunk in _inference_events_to_openai_sse(events):
                    if cancel_ev is not None and cancel_ev.is_set():
                        cancel_codex_for_chat(chat_id or "")
                        return
                    if handle._closed:
                        return
                    yield chunk
            finally:
                handle.close()

        return handle, _iter_codex()

    raise ProviderUnavailable(
        f"Provider {provider_id} cannot stream chat yet",
        provider_id=provider_id,
    )


def _local_proc_alive(bridge) -> Any:
    def _alive() -> bool:
        with bridge._llama._lock:
            p = bridge._llama._proc
        if p is None:
            return True
        return p.poll() is None

    return _alive

"""Narrow chat-completion stream adapter for the agent loop.

Newly written for Accuretta. Lets ``run_chat_turn`` keep orchestration while
LocalLlamaProvider and OpenAIProvider supply OpenAI-compatible SSE bytes.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional, Tuple

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
    # Sampling — shared OpenAI-compatible fields.
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if key in llama_options:
            payload[key] = llama_options[key]
    # max tokens
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

    if use_tools and native_tools and tools_payload:
        payload["tools"] = tools_payload
        payload["tool_choice"] = "auto"

    if provider_id == DEFAULT_PROVIDER_ID:
        # Local llama extras.
        for key, value in llama_options.items():
            payload.setdefault(key, value)
        if chat_template_kwargs:
            payload["chat_template_kwargs"] = chat_template_kwargs
        if stop:
            payload["stop"] = list(stop)
    else:
        # Cloud: strip llama-only keys if present.
        for k in list(payload.keys()):
            if k in {"chat_template_kwargs", "mirostat", "mirostat_tau", "mirostat_eta"}:
                payload.pop(k, None)
    return payload


def open_provider_chat_stream(
    *,
    provider_id: str,
    payload: dict,
    cancel_ev,
    auth_store=None,
    bridge_module=None,
) -> Tuple[Any, Iterator[bytes]]:
    """Open a streaming completion.

    Returns ``(response, raw_sse_byte_iterator)``. Caller must close ``response``.
    The iterator yields the same OpenAI SSE framing both local llama-server and
    OpenAI produce (``data: {...}\\n``), so ``run_chat_turn`` can share parsers.
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
        resp = provider.open_chat_stream(payload, api_key=api_key)

        def _iter_openai():
            # Re-frame parsed objects is unnecessary — read raw SSE bytes.
            while True:
                if cancel_ev is not None and cancel_ev.is_set():
                    return
                chunk = resp.read(1024)
                if not chunk:
                    return
                yield chunk

        return resp, _iter_openai()

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

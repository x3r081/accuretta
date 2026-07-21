"""Local llama.cpp inference provider.

Newly written for Accuretta. Thin, behavior-preserving wrapper around the
existing bridge.py llama-server lifecycle and OpenAI-compatible streaming
APIs. Chat orchestration (tools, truncation, modes) remains in bridge.run_chat_turn;
this provider exposes the same local backend behind the InferenceProvider
interface so cloud providers can plug in later without rewriting local paths.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, List, Optional, Sequence

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
from .errors import ProviderNotConfigured, ProviderUnavailable
from .registry import get_default_registry

LOCAL_LLAMA_DEFINITION = ProviderDefinition(
    id="local_llama",
    display_name="Local llama.cpp",
    api_mode=ApiMode.LOCAL_LLAMA,
    auth_type=AuthType.NONE,
    capabilities=ProviderCapabilities(
        streaming=True,
        tools=True,
        vision=True,
        cancellation=True,
        model_listing=True,
    ),
    default_base_url="http://127.0.0.1:8080",
    supports_model_listing=True,
    experimental=False,
    enabled=True,
)


class LocalLlamaProvider:
    """Delegates to bridge helpers; does not own the llama-server process."""

    def __init__(self, bridge_module: Any = None):
        self._bridge = bridge_module

    @property
    def definition(self) -> ProviderDefinition:
        return LOCAL_LLAMA_DEFINITION

    def _bridge_mod(self):
        if self._bridge is not None:
            return self._bridge
        import bridge as bridge_module

        return bridge_module

    def capabilities(self) -> ProviderCapabilities:
        return self.definition.capabilities

    def validate_configuration(self) -> None:
        bridge = self._bridge_mod()
        settings = bridge.get_settings()
        model_path = (settings.get("model_path") or "").strip()
        if not model_path:
            raise ProviderNotConfigured(
                "No local model configured (settings.model_path is empty)",
                provider_id=self.definition.id,
            )
        if not bridge.safe_exists(model_path):
            raise ProviderNotConfigured(
                "Configured model_path does not exist",
                provider_id=self.definition.id,
            )
        if not bridge.llama_ping(timeout=1.0):
            raise ProviderUnavailable(
                "llama-server is not reachable",
                provider_id=self.definition.id,
            )

    def list_models(self) -> Sequence[dict[str, Any]]:
        bridge = self._bridge_mod()
        settings = bridge.get_settings()
        models_dir = (settings.get("models_dir") or "").strip()
        loaded = ""
        try:
            loaded = bridge._llama.loaded_model() or settings.get("model_path") or ""
        except Exception:
            loaded = settings.get("model_path") or ""

        scanned: List[dict[str, Any]] = []
        if models_dir:
            try:
                scanned = list(bridge.scan_gguf_dir(models_dir) or [])
            except Exception:
                scanned = []

        # Prefer on-disk GGUF scan (matches /api/models). Fall back to live
        # llama-server /v1/models when the directory is empty/unavailable.
        if scanned:
            out = []
            for item in scanned:
                row = dict(item)
                row["loaded"] = row.get("path") == loaded
                row.setdefault("provider_id", self.definition.id)
                out.append(row)
            return out

        try:
            info = bridge.llama_get("/v1/models")
        except Exception as exc:
            raise ProviderUnavailable(
                "Unable to list models from llama-server",
                provider_id=self.definition.id,
            ) from exc
        data = info.get("data") if isinstance(info, dict) else None
        if not isinstance(data, list):
            return []
        rows = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("id") or entry.get("model") or "local"
            rows.append({
                "id": mid,
                "name": mid,
                "provider_id": self.definition.id,
                "loaded": True,
                "path": loaded or "",
            })
        return rows

    def cancel(self, cancellation_id: str) -> bool:
        if not cancellation_id:
            return False
        bridge = self._bridge_mod()
        return bool(bridge.cancel_chat(cancellation_id))

    def stream_response(
        self,
        request: InferenceRequest,
        credentials: Optional[RuntimeCredentials] = None,
    ) -> Iterator[InferenceEvent]:
        """Stream a single OpenAI-compatible completion from llama-server.

        This is the low-level streaming surface. Full agent turns (tools,
        multi-round loops) continue to use bridge.run_chat_turn.
        """
        _ = credentials  # local provider ignores tokens by design
        bridge = self._bridge_mod()
        settings = bridge.get_settings()
        model = request.model or settings.get("model") or "local"

        messages = list(request.messages or [])
        if request.system_prompt:
            messages = [{"role": "system", "content": request.system_prompt}] + messages

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"

        # Merge sampling defaults from settings when caller omitted them.
        opts = bridge.llama_options(settings)
        for key, value in opts.items():
            payload.setdefault(key, value)

        cancel_ev = None
        if request.cancellation_id:
            try:
                with bridge._chat_cancels_lock:
                    entry = bridge._chat_cancels.get(request.cancellation_id) or {}
                cancel_ev = entry.get("cancel")
            except Exception:
                cancel_ev = None

        try:
            resp = bridge.llama_post_stream("/v1/chat/completions", payload)
        except Exception as exc:
            yield InferenceEvent(
                event_type=InferenceEventType.ERROR,
                error="llama-server request failed",
            )
            raise ProviderUnavailable(
                "llama-server request failed",
                provider_id=self.definition.id,
            ) from exc

        try:
            idle = bridge._llama_gen_idle_timeout_s()
            wall = bridge._llama_gen_wall_timeout_s()
            buf = b""
            for chunk in bridge.iter_llama_sse(
                resp,
                idle_timeout=idle,
                wall_timeout=wall,
                cancel_ev=cancel_ev,
            ):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line or not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        yield InferenceEvent(
                            event_type=InferenceEventType.COMPLETED,
                            completion_reason="stop",
                        )
                        return
                    try:
                        obj = json.loads(data.decode("utf-8"))
                    except Exception:
                        continue
                    yield from _events_from_openai_chunk(obj)
        finally:
            try:
                resp.close()
            except Exception:
                pass


def _events_from_openai_chunk(obj: dict) -> Iterator[InferenceEvent]:
    usage = obj.get("usage")
    if isinstance(usage, dict):
        yield InferenceEvent(event_type=InferenceEventType.USAGE, usage=usage, raw=obj)

    choices = obj.get("choices") or []
    if not choices:
        return
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") or {}
    finish = choice.get("finish_reason")

    content = delta.get("content")
    if content:
        yield InferenceEvent(
            event_type=InferenceEventType.TEXT_DELTA,
            text_delta=content,
            raw=obj,
        )

    tool_calls = delta.get("tool_calls")
    if tool_calls:
        yield InferenceEvent(
            event_type=InferenceEventType.TOOL_CALL_DELTA,
            tool_call_delta={"tool_calls": tool_calls},
            raw=obj,
        )

    if finish:
        yield InferenceEvent(
            event_type=InferenceEventType.COMPLETED,
            completion_reason=str(finish),
            raw=obj,
        )


def ensure_local_llama_registered(registry=None) -> None:
    """Attach the LocalLlamaProvider factory to the registry."""
    reg = registry if registry is not None else get_default_registry()
    # Refresh definition (idempotent) and attach factory.
    reg.register(LOCAL_LLAMA_DEFINITION, factory=lambda: LocalLlamaProvider())


# Register factory on import so get_default_registry().get_provider works
# after `import providers.local_llama`.
ensure_local_llama_registered()

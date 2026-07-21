"""OpenAI API-key inference provider.

Newly written for Accuretta. Uses the official OpenAI HTTP Chat Completions
API with a user-supplied API key. No OAuth, ChatGPT subscription, or Hermes
client registrations.
"""

from __future__ import annotations

import codecs
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from auth.models import StoredCredential
from auth.store import AuthStore

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
from .errors import (
    AuthenticationRequired,
    InvalidProviderResponse,
    ProviderUnavailable,
    RateLimited,
)
from .registry import ProviderRegistry, get_default_registry
from .retry import parse_retry_after, with_retries

log = logging.getLogger("accuretta.providers.openai")

OPENAI_PROVIDER_ID = "openai"
OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

# Unique marker used in tests to prove keys never leak.
FAKE_KEY_MARKER = "sk-accuretta-test-OPENAI_KEY_MARKER_9f3c2a"

OPENAI_DEFINITION = ProviderDefinition(
    id=OPENAI_PROVIDER_ID,
    display_name="OpenAI API",
    api_mode=ApiMode.OPENAI_CHAT,
    auth_type=AuthType.API_KEY,
    capabilities=ProviderCapabilities(
        streaming=True,
        tools=True,
        vision=True,
        cancellation=True,
        model_listing=True,
    ),
    default_base_url=OPENAI_DEFAULT_BASE,
    supports_model_listing=True,
    experimental=True,
    enabled=True,
    disabled_reason=None,
)

_CHAT_MODEL_PREFIXES = (
    "gpt-4",
    "gpt-3.5",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "chatgpt",
)

_MODEL_CACHE: Dict[str, Tuple[float, List[dict]]] = {}
_MODEL_CACHE_TTL_S = 60.0


def ensure_openai_registered(registry: Optional[ProviderRegistry] = None) -> None:
    reg = registry if registry is not None else get_default_registry()
    reg.register(OPENAI_DEFINITION, factory=lambda: OpenAIProvider())


def validate_api_key_format(api_key: str) -> Optional[str]:
    if not isinstance(api_key, str):
        return "API key is required"
    key = api_key.strip()
    if not key:
        return "API key is required"
    if any(ch.isspace() for ch in key):
        return "API key must not contain whitespace"
    if len(key) < 20:
        return "API key looks too short"
    lowered = key.lower()
    if lowered in {"sk-...", "your-api-key", "changeme", "xxx"}:
        return "API key looks like a placeholder"
    return None


def api_key_credential(
    api_key: str,
    *,
    validated: bool,
    validated_at: Optional[float] = None,
) -> StoredCredential:
    return StoredCredential(
        provider_id=OPENAI_PROVIDER_ID,
        access_token=api_key.strip(),
        refresh_token=None,
        expires_at=None,
        token_type="api_key",
        scopes=[],
        metadata={
            "credential_type": "api_key",
            "credential_stored": True,
            "credential_validated": bool(validated),
            "validated_at": validated_at,
            "account_label": "OpenAI API key",
        },
    )


def is_chat_model_id(model_id: str) -> bool:
    mid = (model_id or "").strip().lower()
    if not mid:
        return False
    if any(x in mid for x in ("embedding", "whisper", "tts", "dall-e", "davinci-moderation", "transcribe")):
        return False
    return mid.startswith(_CHAT_MODEL_PREFIXES)


def model_capabilities(model_id: str) -> Dict[str, bool]:
    mid = (model_id or "").lower()
    vision = any(x in mid for x in ("gpt-4o", "gpt-4.1", "gpt-5", "vision"))
    tools = not mid.startswith("o1-mini")
    return {"streaming": True, "tools": tools, "vision": vision}


def normalize_usage(usage: Any) -> Optional[dict]:
    """Safe usage fields only — no cost, no raw dump."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    total = usage.get("total_tokens")
    out: dict = {}
    for key, val in (
        ("prompt_tokens", prompt),
        ("completion_tokens", completion),
        ("total_tokens", total),
    ):
        if val is None:
            continue
        try:
            out[key] = int(val)
        except (TypeError, ValueError):
            continue
    if not out:
        return None
    if "total_tokens" not in out and "prompt_tokens" in out and "completion_tokens" in out:
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


class OpenAIProvider:
    """Official OpenAI Chat Completions + Models API via stdlib urllib."""

    def __init__(
        self,
        *,
        base_url: str = OPENAI_DEFAULT_BASE,
        transport: Any = None,
        max_attempts: int = 3,
    ):
        self.base_url = (base_url or OPENAI_DEFAULT_BASE).rstrip("/")
        self._transport = transport
        self.max_attempts = max(1, int(max_attempts))

    @property
    def definition(self) -> ProviderDefinition:
        return OPENAI_DEFINITION

    def capabilities(self) -> ProviderCapabilities:
        return self.definition.capabilities

    def validate_configuration(self) -> None:
        return None

    def _headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _map_http_error(self, status: int, raw: str, *, headers: Optional[dict] = None):
        _ = raw  # never put body into the exception message
        retry_after = None
        if headers:
            retry_after = parse_retry_after(
                headers.get("Retry-After") or headers.get("retry-after")
            )
        if status in (401, 403):
            return AuthenticationRequired(
                "OpenAI rejected the API key or access is forbidden",
                provider_id=OPENAI_PROVIDER_ID,
            )
        if status == 429:
            return RateLimited(
                "OpenAI rate limit exceeded; try again later",
                provider_id=OPENAI_PROVIDER_ID,
                retry_after=retry_after,
            )
        if status in (500, 502, 503, 504) or 500 <= status <= 599:
            return ProviderUnavailable(
                "OpenAI is temporarily unavailable",
                provider_id=OPENAI_PROVIDER_ID,
            )
        return ProviderUnavailable(
            f"OpenAI request failed (HTTP {status})",
            provider_id=OPENAI_PROVIDER_ID,
        )

    def _request_json_once(
        self,
        method: str,
        path: str,
        *,
        api_key: str,
        body: Optional[dict] = None,
        timeout: float = 30.0,
        accept: str = "application/json",
    ) -> Tuple[int, Any]:
        url = f"{self.base_url}{path}"
        headers = self._headers(api_key)
        headers["Accept"] = accept
        if self._transport is not None:
            return self._transport(method, url, headers=headers, body=body, timeout=timeout)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = getattr(resp, "status", 200) or 200
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            hdrs = dict(getattr(exc, "headers", {}) or {})
            raise self._map_http_error(exc.code, raw, headers=hdrs) from None
        except Exception as exc:
            # Never include exception args that might echo request bodies.
            raise ProviderUnavailable(
                f"OpenAI request failed ({type(exc).__name__})",
                provider_id=OPENAI_PROVIDER_ID,
            ) from None
        if not raw:
            return status, {}
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidProviderResponse(
                "OpenAI returned non-JSON",
                provider_id=OPENAI_PROVIDER_ID,
            ) from exc

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        api_key: str,
        body: Optional[dict] = None,
        timeout: float = 30.0,
        accept: str = "application/json",
        cancel_ev=None,
    ) -> Tuple[int, Any]:
        return with_retries(
            lambda: self._request_json_once(
                method, path, api_key=api_key, body=body, timeout=timeout, accept=accept
            ),
            max_attempts=self.max_attempts,
            cancel_ev=cancel_ev,
        )

    def validate_credential(self, api_key: str, *, cancel_ev=None) -> dict:
        status, payload = self._request_json(
            "GET", "/models", api_key=api_key, timeout=20.0, cancel_ev=cancel_ev
        )
        if status != 200:
            raise ProviderUnavailable(
                "OpenAI validation failed",
                provider_id=OPENAI_PROVIDER_ID,
            )
        if not isinstance(payload, dict):
            raise InvalidProviderResponse(
                "Unexpected validation response",
                provider_id=OPENAI_PROVIDER_ID,
            )
        return {"ok": True, "validated": True}

    def list_models(self, *, api_key: str, use_cache: bool = True, cancel_ev=None) -> Sequence[dict]:
        cache_key = "default"
        now = time.time()
        if use_cache and cache_key in _MODEL_CACHE:
            ts, rows = _MODEL_CACHE[cache_key]
            if now - ts < _MODEL_CACHE_TTL_S:
                return list(rows)

        _status, payload = self._request_json(
            "GET", "/models", api_key=api_key, timeout=30.0, cancel_ev=cancel_ev
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise InvalidProviderResponse(
                "OpenAI model list was invalid",
                provider_id=OPENAI_PROVIDER_ID,
            )
        out: List[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("id")
            if not isinstance(mid, str) or not is_chat_model_id(mid):
                continue
            caps = model_capabilities(mid)
            out.append({
                "id": mid,
                "name": mid,
                "provider_id": OPENAI_PROVIDER_ID,
                "capabilities": caps,
            })
        out.sort(key=lambda r: r["id"])
        # Cache only safe model metadata — never credentials.
        _MODEL_CACHE[cache_key] = (now, list(out))
        return out

    def clear_model_cache(self) -> None:
        _MODEL_CACHE.clear()

    def cancel(self, cancellation_id: str) -> bool:
        return False

    def stream_response(
        self,
        request: InferenceRequest,
        credentials: Optional[RuntimeCredentials] = None,
        *,
        cancel_ev=None,
    ) -> Iterator[InferenceEvent]:
        api_key = (credentials.access_token if credentials else None) or ""
        if not api_key:
            raise AuthenticationRequired(
                "OpenAI API key required",
                provider_id=OPENAI_PROVIDER_ID,
            )
        payload = self._build_chat_payload(request)
        resp = self.open_chat_stream(payload, api_key=api_key, cancel_ev=cancel_ev)
        try:
            for obj in self.iter_openai_sse_objects(resp, cancel_ev=cancel_ev):
                yield from self._events_from_chunk(obj)
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _build_chat_payload(self, request: InferenceRequest) -> dict:
        messages = list(request.messages or [])
        if request.system_prompt:
            messages = [{"role": "system", "content": request.system_prompt}] + messages
        payload: dict = {
            "model": request.model or OPENAI_DEFAULT_MODEL,
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
        return payload

    def open_chat_stream(
        self,
        payload: dict,
        *,
        api_key: str,
        timeout: float = 120.0,
        cancel_ev=None,
    ):
        """Open a streaming chat.completions response (caller must close).

        Retries only apply before the response object is returned — never after
        partial streamed output has begun.
        """

        def _open_once():
            return self._open_chat_stream_once(payload, api_key=api_key, timeout=timeout)

        return with_retries(
            _open_once,
            max_attempts=self.max_attempts,
            cancel_ev=cancel_ev,
        )

    def _open_chat_stream_once(self, payload: dict, *, api_key: str, timeout: float = 120.0):
        url = f"{self.base_url}/chat/completions"
        body = dict(payload)
        body["stream"] = True
        body.setdefault("stream_options", {"include_usage": True})
        headers = self._headers(api_key)
        headers["Accept"] = "text/event-stream"
        if self._transport is not None:
            status, resp = self._transport(
                "POST", url, headers=headers, body=body, timeout=timeout, stream=True
            )
            if status >= 400:
                raise self._map_http_error(status, "")
            return resp
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            hdrs = dict(getattr(exc, "headers", {}) or {})
            try:
                exc.close()
            except Exception:
                pass
            raise self._map_http_error(exc.code, raw, headers=hdrs) from None
        except Exception as exc:
            raise ProviderUnavailable(
                f"OpenAI stream failed ({type(exc).__name__})",
                provider_id=OPENAI_PROVIDER_ID,
            ) from None

    def iter_openai_sse_objects(self, resp, *, cancel_ev=None) -> Iterator[dict]:
        """Yield parsed JSON objects from an OpenAI SSE body.

        Hardened for blank lines, multi-line ``data:`` fields, split UTF-8,
        malformed JSON (skipped), and early cancellation / connection close.
        """
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text_buf = ""
        data_lines: List[str] = []

        def _flush_event() -> Optional[dict]:
            nonlocal data_lines
            if not data_lines:
                return None
            payload = "\n".join(data_lines).strip()
            data_lines = []
            if not payload:
                return None
            if payload == "[DONE]":
                return {"__done__": True}
            try:
                obj = json.loads(payload)
            except Exception:
                return None
            return obj if isinstance(obj, dict) else None

        while True:
            if cancel_ev is not None and cancel_ev.is_set():
                return
            try:
                chunk = resp.read(1024)
            except Exception:
                # Premature close — stop without leaking details.
                return
            if not chunk:
                # Final flush
                event = _flush_event()
                if event and not event.get("__done__"):
                    yield event
                return
            text_buf += decoder.decode(chunk)
            while True:
                if cancel_ev is not None and cancel_ev.is_set():
                    return
                nl = text_buf.find("\n")
                if nl < 0:
                    break
                line, text_buf = text_buf[:nl], text_buf[nl + 1:]
                if line.endswith("\r"):
                    line = line[:-1]
                if line == "":
                    event = _flush_event()
                    if event is None:
                        continue
                    if event.get("__done__"):
                        return
                    yield event
                    continue
                if line.startswith(":"):
                    # SSE comment
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    # OpenAI usually sends one JSON object per data line.
                    # Flush early when the accumulated payload is complete JSON
                    # or [DONE]; otherwise wait for a blank line (multi-line data).
                    payload = "\n".join(data_lines).strip()
                    if payload == "[DONE]":
                        data_lines = []
                        return
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    data_lines = []
                    if isinstance(obj, dict):
                        yield obj
                    continue
                # Ignore other SSE fields (event:, id:, retry:)
        # unreachable

    def _events_from_chunk(self, obj: dict) -> Iterator[InferenceEvent]:
        usage = normalize_usage(obj.get("usage"))
        if usage:
            yield InferenceEvent(event_type=InferenceEventType.USAGE, usage=usage, raw=None)
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
            )
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            # Pass through multi-index tool-call fragments for the agent loop
            # to assemble; never attach raw upstream object.
            yield InferenceEvent(
                event_type=InferenceEventType.TOOL_CALL_DELTA,
                tool_call_delta={"tool_calls": tool_calls},
            )
        if finish:
            yield InferenceEvent(
                event_type=InferenceEventType.COMPLETED,
                completion_reason=str(finish),
            )


def connect_openai_api_key(store: AuthStore, api_key: str, *, provider: Optional[OpenAIProvider] = None) -> dict:
    """Validate (when possible) and store an API key. Never logs the key."""
    err = validate_api_key_format(api_key)
    if err:
        raise AuthenticationRequired(err, provider_id=OPENAI_PROVIDER_ID)
    prov = provider or OpenAIProvider()
    validated = False
    try:
        prov.validate_credential(api_key.strip())
        validated = True
    except RateLimited:
        cred = api_key_credential(api_key, validated=False, validated_at=None)
        store.save(OPENAI_PROVIDER_ID, cred)
        raise
    except ProviderUnavailable:
        cred = api_key_credential(api_key, validated=False, validated_at=None)
        store.save(OPENAI_PROVIDER_ID, cred)
        raise ProviderUnavailable(
            "Could not validate the API key with OpenAI right now; key was stored locally",
            provider_id=OPENAI_PROVIDER_ID,
        ) from None
    except AuthenticationRequired:
        raise

    cred = api_key_credential(
        api_key,
        validated=validated,
        validated_at=time.time() if validated else None,
    )
    store.save(OPENAI_PROVIDER_ID, cred)
    return {
        "ok": True,
        "credentialStored": True,
        "credentialValidated": validated,
    }


def openai_api_key_from_store(store: AuthStore) -> Optional[str]:
    cred = store.load(OPENAI_PROVIDER_ID)
    if cred is None:
        return None
    key = cred.access_token
    if not key:
        return None
    return key

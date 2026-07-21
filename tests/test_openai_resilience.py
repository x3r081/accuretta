"""OpenAI resilience: retries, stream hardening, redaction (mocked — no network)."""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.base import InferenceEventType, InferenceRequest, RuntimeCredentials
from providers.errors import AuthenticationRequired, ProviderError, ProviderUnavailable, RateLimited
from providers.openai_provider import (
    FAKE_KEY_MARKER,
    OpenAIProvider,
    normalize_usage,
)
from providers.retry import cancellable_sleep, compute_backoff_seconds, parse_retry_after, with_retries


class _CloseableResp:
    def __init__(self, data: bytes = b""):
        self._data = data
        self._i = 0
        self.closed = False

    def read(self, n=1024):
        if self.closed:
            return b""
        if self._i >= len(self._data):
            return b""
        out = self._data[self._i : self._i + n]
        self._i += n
        return out

    def close(self):
        self.closed = True


class RetryHelpersTest(unittest.TestCase):
    def test_parse_retry_after_capped(self):
        self.assertEqual(parse_retry_after("2"), 2.0)
        self.assertEqual(parse_retry_after("999", cap=30.0), 30.0)
        self.assertIsNone(parse_retry_after("nope"))

    def test_backoff_honors_retry_after(self):
        d = compute_backoff_seconds(0, retry_after=1.5, cap=8.0, rng=None)
        self.assertEqual(d, 1.5)

    def test_cancel_during_backoff(self):
        set_ev = threading.Event()
        set_ev.set()
        self.assertFalse(cancellable_sleep(5.0, set_ev))

        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise RateLimited("slow", retry_after=10.0)

        # Cancel happens while waiting between attempts (sleeper returns False).
        with self.assertRaises(ProviderError):
            with_retries(fn, max_attempts=5, cancel_ev=threading.Event(), sleeper=lambda s: False)
        self.assertEqual(calls["n"], 1)


class OpenAIRetryTest(unittest.TestCase):
    def test_retry_on_initial_429(self):
        attempts = {"n": 0}

        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RateLimited("slow", provider_id="openai", retry_after=0.01)
            return (200, {"data": [{"id": "gpt-4o-mini"}]})

        prov = OpenAIProvider(transport=transport, max_attempts=3)
        with mock.patch("providers.retry.cancellable_sleep", return_value=True):
            status, payload = prov._request_json("GET", "/models", api_key=FAKE_KEY_MARKER)
        self.assertEqual(status, 200)
        self.assertEqual(attempts["n"], 2)

    def test_bounded_retry_on_5xx(self):
        attempts = {"n": 0}

        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            attempts["n"] += 1
            raise ProviderUnavailable("down", provider_id="openai")

        prov = OpenAIProvider(transport=transport, max_attempts=3)
        with mock.patch("providers.retry.cancellable_sleep", return_value=True):
            with self.assertRaises(ProviderUnavailable):
                prov._request_json("GET", "/models", api_key=FAKE_KEY_MARKER)
        self.assertEqual(attempts["n"], 3)

    def test_retry_after_from_headers(self):
        prov = OpenAIProvider()
        err = prov._map_http_error(429, "x", headers={"Retry-After": "2"})
        self.assertIsInstance(err, RateLimited)
        self.assertEqual(err.retry_after, 2.0)

    def test_no_retry_on_401(self):
        attempts = {"n": 0}

        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            attempts["n"] += 1
            raise AuthenticationRequired("bad", provider_id="openai")

        prov = OpenAIProvider(transport=transport, max_attempts=5)
        with self.assertRaises(AuthenticationRequired):
            prov._request_json("GET", "/models", api_key=FAKE_KEY_MARKER)
        self.assertEqual(attempts["n"], 1)

    def test_no_retry_after_partial_output(self):
        """Once stream is open, retries must not re-open the connection."""
        opens = {"n": 0}
        sse = (
            b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
            b"data: [DONE]\n\n"
        )

        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            opens["n"] += 1
            if opens["n"] == 1:
                return (200, _CloseableResp(sse))
            raise AssertionError("must not retry after stream opened")

        prov = OpenAIProvider(transport=transport, max_attempts=3)
        events = list(
            prov.stream_response(
                InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]),
                RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
            )
        )
        self.assertEqual(opens["n"], 1)
        self.assertTrue(any(e.event_type == InferenceEventType.TEXT_DELTA for e in events))

    def test_stream_open_retries_before_bytes(self):
        opens = {"n": 0}
        sse = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n' b"data: [DONE]\n\n"

        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            opens["n"] += 1
            if opens["n"] < 2:
                raise RateLimited("slow", provider_id="openai", retry_after=0.01)
            return (200, _CloseableResp(sse))

        prov = OpenAIProvider(transport=transport, max_attempts=3)
        with mock.patch("providers.retry.cancellable_sleep", return_value=True):
            resp = prov.open_chat_stream({"model": "gpt-4o-mini", "messages": []}, api_key=FAKE_KEY_MARKER)
        self.assertEqual(opens["n"], 2)
        resp.close()

    def test_secret_redaction_during_retries(self):
        logs = []

        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            # Simulate a buggy logger capturing headers — our code must never
            # put the key into exception messages.
            raise ProviderUnavailable("OpenAI is temporarily unavailable", provider_id="openai")

        prov = OpenAIProvider(transport=transport, max_attempts=2)
        with mock.patch("providers.retry.cancellable_sleep", return_value=True):
            with self.assertRaises(ProviderUnavailable) as ctx:
                prov._request_json("GET", "/models", api_key=FAKE_KEY_MARKER)
        msg = str(ctx.exception)
        self.assertNotIn(FAKE_KEY_MARKER, msg)
        self.assertNotIn("Authorization", msg)
        self.assertNotIn("Bearer", msg)


class OpenAIStreamHardeningTest(unittest.TestCase):
    def test_response_close_after_completion(self):
        sse = b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\n\n' b"data: [DONE]\n\n"
        resp = _CloseableResp(sse)

        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            return (200, resp)

        prov = OpenAIProvider(transport=transport)
        list(
            prov.stream_response(
                InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]),
                RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
            )
        )
        self.assertTrue(resp.closed)

    def test_response_close_after_cancellation(self):
        # Large body so we cancel mid-read after first chunk.
        piece = b'data: {"choices":[{"delta":{"content":"aaaa"}}]}\n\n'
        sse = piece * 50
        resp = _CloseableResp(sse)
        cancel = threading.Event()

        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            return (200, resp)

        prov = OpenAIProvider(transport=transport)
        events = []
        gen = prov.stream_response(
            InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]),
            RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
            cancel_ev=cancel,
        )
        for e in gen:
            events.append(e)
            cancel.set()
            break
        # Exhaust / close via generator close
        gen.close()
        self.assertTrue(resp.closed)

    def test_split_utf8_stream_data(self):
        # "café" split across reads mid-codepoint
        text = "café"
        payload = json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False)
        full = f"data: {payload}\n\ndata: [DONE]\n\n".encode("utf-8")
        # Split so multi-byte é is cut
        mid = full.index("é".encode("utf-8")) + 1
        chunks = [full[:mid], full[mid:]]

        class _SplitResp:
            def __init__(self):
                self._chunks = list(chunks)
                self.closed = False

            def read(self, n=1024):
                if not self._chunks:
                    return b""
                return self._chunks.pop(0)

            def close(self):
                self.closed = True

        resp = _SplitResp()
        prov = OpenAIProvider(transport=lambda *a, **k: (200, resp))
        events = list(
            prov.stream_response(
                InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]),
                RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
            )
        )
        deltas = "".join(e.text_delta or "" for e in events if e.event_type == InferenceEventType.TEXT_DELTA)
        self.assertEqual(deltas, "café")
        self.assertTrue(resp.closed)

    def test_multiple_tool_calls(self):
        chunks = [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a","function":{"name":"t1","arguments":"{"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"b","function":{"name":"t2","arguments":"{"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        resp = _CloseableResp(b"".join(chunks))
        prov = OpenAIProvider(transport=lambda *a, **k: (200, resp))
        events = list(
            prov.stream_response(
                InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]),
                RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
            )
        )
        tool_evs = [e for e in events if e.event_type == InferenceEventType.TOOL_CALL_DELTA]
        self.assertGreaterEqual(len(tool_evs), 2)
        indexes = []
        for e in tool_evs:
            for tc in (e.tool_call_delta or {}).get("tool_calls") or []:
                indexes.append(tc.get("index"))
        self.assertIn(0, indexes)
        self.assertIn(1, indexes)

    def test_incomplete_tool_call(self):
        # Stream ends without finish_reason; incomplete args — no crash.
        chunks = [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"f","arguments":"{\\"p\\":"}}]}}]}\n\n',
        ]
        resp = _CloseableResp(b"".join(chunks))
        prov = OpenAIProvider(transport=lambda *a, **k: (200, resp))
        events = list(
            prov.stream_response(
                InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]),
                RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
            )
        )
        self.assertTrue(any(e.event_type == InferenceEventType.TOOL_CALL_DELTA for e in events))
        self.assertTrue(resp.closed)

    def test_usage_normalization(self):
        self.assertEqual(
            normalize_usage({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}),
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )
        self.assertEqual(
            normalize_usage({"input_tokens": 4, "output_tokens": 5}),
            {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
        )
        self.assertIsNone(normalize_usage(None))
        self.assertNotIn("cost", normalize_usage({"prompt_tokens": 1, "cost": 9.9}) or {})

        sse = (
            b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":10,"completion_tokens":3},"choices":[]}\n\n'
            b"data: [DONE]\n\n"
        )
        resp = _CloseableResp(sse)
        prov = OpenAIProvider(transport=lambda *a, **k: (200, resp))
        events = list(
            prov.stream_response(
                InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]),
                RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
            )
        )
        usage_ev = [e for e in events if e.event_type == InferenceEventType.USAGE]
        self.assertEqual(len(usage_ev), 1)
        self.assertEqual(usage_ev[0].usage["prompt_tokens"], 10)
        self.assertEqual(usage_ev[0].usage["completion_tokens"], 3)
        self.assertEqual(usage_ev[0].usage["total_tokens"], 13)
        self.assertIsNone(usage_ev[0].raw)

    def test_premature_stream_closure(self):
        class _Boom:
            closed = False

            def read(self, n=1024):
                raise ConnectionError("reset")

            def close(self):
                self.closed = True

        boom = _Boom()
        prov = OpenAIProvider(transport=lambda *a, **k: (200, boom))
        events = list(
            prov.stream_response(
                InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]),
                RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
            )
        )
        self.assertEqual(events, [])
        self.assertTrue(boom.closed)

    def test_malformed_json_skipped(self):
        sse = (
            b"data: {not-json}\n\n"
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        resp = _CloseableResp(sse)
        prov = OpenAIProvider(transport=lambda *a, **k: (200, resp))
        events = list(
            prov.stream_response(
                InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]),
                RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
            )
        )
        deltas = "".join(e.text_delta or "" for e in events if e.event_type == InferenceEventType.TEXT_DELTA)
        self.assertEqual(deltas, "ok")
        for e in events:
            blob = json.dumps(e.raw) if e.raw else ""
            self.assertNotIn("not-json", blob)


if __name__ == "__main__":
    unittest.main()

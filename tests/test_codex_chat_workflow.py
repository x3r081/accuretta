"""Codex chat workflow: routing, streaming, cancel, isolation, no fallback."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex.discover import reset_discovery_cache
from codex.flags import ENV_CODEX_INFERENCE_ENABLED
from codex.inference import CodexInferenceService
from codex.session import CodexSession, reset_codex_session
from providers.base import InferenceEventType, InferenceRequest
from providers.codex_errors import (
    MSG_SIGN_IN_EXPIRED,
    MSG_THREAD_INVALID,
    MSG_TURN_CANCELLED,
    user_message_for_codex_error,
)
from providers.codex_provider import (
    CODEX_DISPLAY_NAME,
    CODEX_PROVIDER_ID,
    LOCAL_DISPLAY_NAME,
    CodexProvider,
    bind_codex_thread,
    cancel_codex_for_chat,
    clear_codex_thread_for_chat,
    get_bound_codex_thread,
    pop_codex_turn_meta,
)
from providers.errors import ProviderUnavailable
from providers.inference_stream import open_provider_chat_stream
from providers.management import ensure_builtin_providers, reset_auth_store_info
from providers.registry import reset_default_registry
from providers.selection import DEFAULT_PROVIDER_ID, resolve_provider_selection
from providers.status import assert_safe_provider_payload

FAKE_SERVER = Path(__file__).resolve().parent / "fake_codex_app_server.py"
SECRET = "sk-fake-SECRET-marker-do-not-leak"
ROOT = Path(__file__).resolve().parent.parent


def _make_fake_codex_bin(tmp: Path, *, mode: str = "authenticated") -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    wrapper = tmp / "codex"
    py = sys.executable
    script = FAKE_SERVER
    wrapper.write_text(
        "#!/bin/sh\n"
        f"export FAKE_CODEX_MODE={mode}\n"
        f"export FAKE_CODEX_SECRET_MARKER={SECRET}\n"
        f'exec "{py}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


class CodexChatWorkflowTest(unittest.TestCase):
    def setUp(self):
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = _make_fake_codex_bin(Path(self.tmp.name), mode="authenticated")
        self._env = mock.patch.dict(
            os.environ,
            {
                "ACCURETTA_CODEX_BIN": str(self.bin),
                ENV_CODEX_INFERENCE_ENABLED: "1",
                "FAKE_CODEX_TURN_DELAY_MS": "20",
            },
            clear=False,
        )
        self._env.start()
        ensure_builtin_providers()
        clear_codex_thread_for_chat("c1")
        clear_codex_thread_for_chat("c2")

    def tearDown(self):
        clear_codex_thread_for_chat("c1")
        clear_codex_thread_for_chat("c2")
        try:
            reset_codex_session()
        except Exception:
            pass
        self._env.stop()
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        reset_auth_store_info()
        self.tmp.cleanup()

    def test_selected_local_routes_locally(self):
        sel = resolve_provider_selection({"provider_id": DEFAULT_PROVIDER_ID})
        self.assertEqual(sel.provider_id, DEFAULT_PROVIDER_ID)
        local_hits = []

        class FB:
            def llama_post_stream(self, *a, **k):
                local_hits.append(1)

                class R:
                    def close(self):
                        pass

                    def read(self, n=0):
                        return b""

                return R()

            def iter_llama_sse(self, *a, **k):
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                yield b"data: [DONE]\n\n"

            def _llama_gen_idle_timeout_s(self):
                return 1

            def _llama_gen_wall_timeout_s(self):
                return 1

            class _llama:
                _lock = threading.Lock()
                _proc = None

        handle, it = open_provider_chat_stream(
            provider_id=DEFAULT_PROVIDER_ID,
            payload={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
            cancel_ev=threading.Event(),
            bridge_module=FB(),
            chat_id="local-1",
        )
        text = b"".join(it).decode("utf-8")
        self.assertEqual(local_hits, [1])
        self.assertIn("hi", text)
        handle.close()

    def test_selected_codex_routes_to_codex(self):
        local_hits = []

        class FB:
            def llama_post_stream(self, *a, **k):
                local_hits.append(1)
                raise AssertionError("must not use local")

        handle, it = open_provider_chat_stream(
            provider_id=CODEX_PROVIDER_ID,
            payload={"model": "codex", "messages": [{"role": "user", "content": "hi"}]},
            cancel_ev=threading.Event(),
            bridge_module=FB(),
            chat_id="c1",
            correlation_id="c1:t1",
        )
        text = b"".join(it).decode("utf-8")
        self.assertEqual(local_hits, [])
        self.assertIn("Hello", text)
        self.assertIn("Codex", text)
        handle.close()
        meta = pop_codex_turn_meta("c1")
        self.assertIsNotNone(meta)
        self.assertEqual(meta["providerId"], CODEX_PROVIDER_ID)
        self.assertEqual(meta["providerDisplayName"], CODEX_DISPLAY_NAME)
        self.assertTrue(meta.get("threadId"))
        assert_safe_provider_payload(
            {k: v for k, v in meta.items() if k not in {"threadId", "turnId", "correlationId", "createdThread"}}
        )

    def test_stream_updates_one_assistant_and_completes(self):
        provider = CodexProvider()
        req = InferenceRequest(
            model="codex",
            messages=[{"role": "user", "content": "hi"}],
            cancellation_id="c1",
            extra={"chat_id": "c1", "correlation_id": "c1:one"},
        )
        deltas = []
        completed = None
        for evt in provider.stream_response(req):
            if evt.event_type == InferenceEventType.TEXT_DELTA and evt.text_delta:
                deltas.append(evt.text_delta)
            if evt.event_type == InferenceEventType.COMPLETED:
                completed = evt
        self.assertEqual("".join(deltas), "Hello from Codex")
        self.assertIsNotNone(completed)
        self.assertEqual(completed.completion_reason, "stop")
        self.assertEqual(completed.raw.get("providerDisplayName"), CODEX_DISPLAY_NAME)

    def test_cancellation_stops_turn(self):
        os.environ["FAKE_CODEX_TURN_DELAY_MS"] = "400"
        provider = CodexProvider()
        req = InferenceRequest(
            model="codex",
            messages=[{"role": "user", "content": "slow"}],
            cancellation_id="c1",
            extra={"chat_id": "c1"},
        )
        box = {}

        def _run():
            box["events"] = list(provider.stream_response(req))

        th = threading.Thread(target=_run)
        th.start()
        time.sleep(0.12)
        self.assertTrue(cancel_codex_for_chat("c1"))
        th.join(timeout=5)
        kinds = [e.event_type for e in box["events"]]
        self.assertIn(InferenceEventType.COMPLETED, kinds)
        done = [e for e in box["events"] if e.event_type == InferenceEventType.COMPLETED][-1]
        self.assertEqual(done.completion_reason, "cancelled")

    def test_stale_events_ignored_by_service(self):
        os.environ["FAKE_CODEX_EMIT_STALE_TURN"] = "1"
        try:
            provider = CodexProvider()
            req = InferenceRequest(
                model="codex",
                messages=[{"role": "user", "content": "hi"}],
                cancellation_id="c1",
            )
            text = "".join(
                e.text_delta or ""
                for e in provider.stream_response(req)
                if e.event_type == InferenceEventType.TEXT_DELTA
            )
            self.assertEqual(text, "Hello from Codex")
            self.assertNotIn("STALE", text)
        finally:
            os.environ.pop("FAKE_CODEX_EMIT_STALE_TURN", None)

    def test_duplicate_submission_prevented(self):
        os.environ["FAKE_CODEX_TURN_DELAY_MS"] = "300"
        provider = CodexProvider()
        req = InferenceRequest(
            model="codex",
            messages=[{"role": "user", "content": "one"}],
            cancellation_id="c1",
            extra={"chat_id": "c1"},
        )
        started = threading.Event()
        errors = []

        def _first():
            started.set()
            list(provider.stream_response(req))

        th = threading.Thread(target=_first)
        th.start()
        self.assertTrue(started.wait(2))
        time.sleep(0.05)
        try:
            list(
                CodexProvider().stream_response(
                    InferenceRequest(
                        model="codex",
                        messages=[{"role": "user", "content": "two"}],
                        cancellation_id="c1",
                        extra={"chat_id": "c1"},
                    )
                )
            )
        except ProviderUnavailable as exc:
            errors.append(exc.message)
        th.join(timeout=5)
        self.assertTrue(errors)
        self.assertIn("already in progress", errors[0].lower())

    def test_invalid_thread_recovers(self):
        bind_codex_thread("c1", "does-not-exist-thread")
        provider = CodexProvider()
        req = InferenceRequest(
            model="codex",
            messages=[{"role": "user", "content": "recover"}],
            cancellation_id="c1",
            extra={"chat_id": "c1", "thread_id": "does-not-exist-thread"},
        )
        text = "".join(
            e.text_delta or ""
            for e in provider.stream_response(req)
            if e.event_type == InferenceEventType.TEXT_DELTA
        )
        self.assertEqual(text, "Hello from Codex")
        self.assertNotEqual(get_bound_codex_thread("c1"), "does-not-exist-thread")

    def test_expired_auth_handled(self):
        bin_u = _make_fake_codex_bin(Path(self.tmp.name) / "u", mode="ok")
        with mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(bin_u)}):
            reset_discovery_cache()
            reset_codex_session()
            with self.assertRaises(Exception) as ctx:
                open_provider_chat_stream(
                    provider_id=CODEX_PROVIDER_ID,
                    payload={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                    cancel_ev=threading.Event(),
                    chat_id="c1",
                )
        self.assertEqual(ctx.exception.message, MSG_SIGN_IN_EXPIRED)

    def test_no_automatic_local_fallback(self):
        local_hits = []

        class FB:
            def llama_post_stream(self, *a, **k):
                local_hits.append(1)
                raise AssertionError("fallback")

        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            reset_discovery_cache()
            with self.assertRaises(ProviderUnavailable):
                open_provider_chat_stream(
                    provider_id=CODEX_PROVIDER_ID,
                    payload={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                    cancel_ev=threading.Event(),
                    bridge_module=FB(),
                    chat_id="c1",
                )
        self.assertEqual(local_hits, [])

    def test_conversation_thread_isolation(self):
        p = CodexProvider()
        for cid, prompt in (("c1", "a"), ("c2", "b")):
            list(
                p.stream_response(
                    InferenceRequest(
                        model="codex",
                        messages=[{"role": "user", "content": prompt}],
                        cancellation_id=cid,
                        extra={"chat_id": cid},
                    )
                )
            )
        t1 = get_bound_codex_thread("c1")
        t2 = get_bound_codex_thread("c2")
        self.assertTrue(t1)
        self.assertTrue(t2)
        self.assertNotEqual(t1, t2)

    def test_provider_labels(self):
        self.assertEqual(CODEX_DISPLAY_NAME, "Codex via ChatGPT")
        self.assertEqual(LOCAL_DISPLAY_NAME, "Local llama.cpp")
        js = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('return "Codex via ChatGPT"', js)
        self.assertIn('return "Local llama.cpp"', js)
        self.assertIn('evt.type === "provider"', js)
        self.assertIn("provider_label", js)

    def test_ui_guards_duplicate_and_preflight(self):
        js = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("if (state.streaming) return;", js)
        self.assertIn("codex_chatgpt", js)
        self.assertIn("selectable", js)
        self.assertIn("evt.chat_id !== state.chatId", js)

    def test_cancelled_user_message(self):
        self.assertEqual(
            user_message_for_codex_error(Exception("x"), cancelled=True),
            MSG_TURN_CANCELLED,
        )
        self.assertIn("thread", MSG_THREAD_INVALID.lower())

    def test_resolve_codex_inference_model_ignores_local_llama(self):
        from providers.codex_errors import MSG_UNSUPPORTED_MODEL, is_unsupported_model_message
        from providers.codex_provider import resolve_codex_inference_model

        self.assertIsNone(
            resolve_codex_inference_model(
                {"model": "qwen2.5-coder-14b-instruct-q4_k_m", "codex_model": ""}
            )
        )
        self.assertIsNone(resolve_codex_inference_model(requested="codex"))
        self.assertIsNone(
            resolve_codex_inference_model(
                requested="/Users/johan/models/foo.gguf"
            )
        )
        self.assertIsNone(
            resolve_codex_inference_model(requested="qwen2.5-coder-14b-instruct-q4_k_m")
        )
        self.assertEqual(
            resolve_codex_inference_model({"codex_model": "gpt-5.1-codex"}),
            "gpt-5.1-codex",
        )
        raw = (
            '{"type":"error","error":{"message":"The \'qwen2.5-coder-14b-instruct-q4_k_m\' '
            'model is not supported when using Codex with a ChatGPT account."}}'
        )
        self.assertTrue(is_unsupported_model_message(raw))
        self.assertEqual(
            user_message_for_codex_error(Exception(raw)),
            MSG_UNSUPPORTED_MODEL,
        )

    def test_stream_does_not_forward_local_model_to_thread_start(self):
        provider = CodexProvider()
        seen = {}

        real_create = CodexInferenceService.create_thread

        def _wrap(self, *args, **kwargs):
            seen["model"] = kwargs.get("model", args[0] if args else "MISSING")
            return real_create(self, *args, **kwargs)

        with mock.patch.object(CodexInferenceService, "create_thread", _wrap):
            req = InferenceRequest(
                model="qwen2.5-coder-14b-instruct-q4_k_m",
                messages=[{"role": "user", "content": "hi"}],
                cancellation_id="c-local-model",
                extra={"chat_id": "c-local-model"},
            )
            events = list(provider.stream_response(req))
        self.assertEqual(seen.get("model"), None)
        self.assertTrue(any(e.event_type == InferenceEventType.COMPLETED for e in events))

    def test_stream_yields_deltas_before_turn_completes(self):
        """Regression: Stop / live UI need deltas during the turn, not only at end."""
        os.environ["FAKE_CODEX_TURN_DELAY_MS"] = "250"
        provider = CodexProvider()
        req = InferenceRequest(
            model="",
            messages=[{"role": "user", "content": "stream-me"}],
            cancellation_id="c-live",
            extra={"chat_id": "c-live"},
        )
        saw_delta_before_done = False
        completed = False
        for evt in provider.stream_response(req):
            if evt.event_type == InferenceEventType.TEXT_DELTA and evt.text_delta:
                if not completed:
                    saw_delta_before_done = True
            if evt.event_type == InferenceEventType.COMPLETED:
                completed = True
        self.assertTrue(saw_delta_before_done)
        self.assertTrue(completed)


if __name__ == "__main__":
    unittest.main()

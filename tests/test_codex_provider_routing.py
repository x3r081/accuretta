"""Codex as an internal inference provider — routing, readiness, no fallback."""

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
from codex.session import reset_codex_session
from providers.base import InferenceEventType, InferenceRequest
from providers.codex_provider import (
    CODEX_PROVIDER_ID,
    CodexProvider,
    cancel_codex_for_chat,
    clear_codex_thread_for_chat,
    ensure_codex_registered,
    get_codex_provider_status,
    safe_codex_response_metadata,
)
from providers.codex_readiness import (
    ENV_PROTOCOL_SUPPORTED,
    CodexInferenceStatus,
    assess_codex_inference_readiness,
)
from providers.errors import AuthenticationRequired, ProviderUnavailable
from providers.inference_stream import open_provider_chat_stream
from providers.management import (
    ensure_builtin_providers,
    reset_auth_store_info,
    resolve_chat_provider,
    select_provider,
)
from providers.registry import reset_default_registry
from providers.selection import (
    DEFAULT_PROVIDER_ID,
    assert_provider_usable_for_chat,
    resolve_provider_selection,
)
from providers.status import assert_safe_provider_payload

FAKE_SERVER = Path(__file__).resolve().parent / "fake_codex_app_server.py"
SECRET = "sk-fake-SECRET-marker-do-not-leak"


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


class CodexReadinessTest(unittest.TestCase):
    def setUp(self):
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = _make_fake_codex_bin(Path(self.tmp.name), mode="authenticated")
        self._env = mock.patch.dict(
            os.environ,
            {
                "ACCURETTA_CODEX_BIN": str(self.bin),
                ENV_CODEX_INFERENCE_ENABLED: "1",
            },
            clear=False,
        )
        self._env.start()
        os.environ.pop(ENV_PROTOCOL_SUPPORTED, None)

    def tearDown(self):
        self._env.stop()
        os.environ.pop(ENV_PROTOCOL_SUPPORTED, None)
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        self.tmp.cleanup()

    def test_ready_when_flag_cli_auth_process_ok(self):
        r = assess_codex_inference_readiness(live=True)
        self.assertTrue(r["ready"])
        self.assertEqual(r["status"], CodexInferenceStatus.READY.value)
        self.assertTrue(r["flagEnabled"])
        self.assertTrue(r["cliAvailable"])
        self.assertTrue(r["authenticated"])
        self.assertTrue(r["processReady"])
        self.assertTrue(r["protocolSupported"])
        self.assertIsNone(r["reason"])
        assert_safe_provider_payload(r)

    def test_inference_disabled(self):
        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            r = assess_codex_inference_readiness(live=False)
        self.assertFalse(r["ready"])
        self.assertEqual(r["status"], CodexInferenceStatus.INFERENCE_DISABLED.value)
        self.assertFalse(r["flagEnabled"])

    def test_cli_missing(self):
        with mock.patch.dict(
            os.environ,
            {"ACCURETTA_CODEX_BIN": "/nonexistent/codex-missing"},
            clear=False,
        ):
            reset_discovery_cache()
            r = assess_codex_inference_readiness(live=False)
        self.assertFalse(r["ready"])
        self.assertEqual(r["status"], CodexInferenceStatus.CLI_MISSING.value)

    def test_not_signed_in(self):
        bin_u = _make_fake_codex_bin(Path(self.tmp.name) / "u", mode="ok")
        with mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(bin_u)}):
            reset_discovery_cache()
            reset_codex_session()
            r = assess_codex_inference_readiness(live=True)
        self.assertFalse(r["ready"])
        self.assertEqual(r["status"], CodexInferenceStatus.NOT_SIGNED_IN.value)

    def test_protocol_unsupported(self):
        with mock.patch.dict(os.environ, {ENV_PROTOCOL_SUPPORTED: "0"}):
            r = assess_codex_inference_readiness(live=False)
        self.assertFalse(r["ready"])
        self.assertEqual(r["status"], CodexInferenceStatus.PROTOCOL_UNSUPPORTED.value)
        self.assertFalse(r["protocolSupported"])


class CodexProviderRoutingTest(unittest.TestCase):
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
        os.environ.pop(ENV_PROTOCOL_SUPPORTED, None)
        ensure_builtin_providers()

    def tearDown(self):
        clear_codex_thread_for_chat("chat-1")
        clear_codex_thread_for_chat("chat-dual")
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

    def test_local_provider_still_resolves(self):
        sel = resolve_provider_selection({"provider_id": DEFAULT_PROVIDER_ID})
        self.assertEqual(sel.provider_id, DEFAULT_PROVIDER_ID)
        assert_provider_usable_for_chat(sel)
        chat = resolve_chat_provider({"provider_id": DEFAULT_PROVIDER_ID})
        self.assertEqual(chat.provider_id, DEFAULT_PROVIDER_ID)

    def test_settings_select_still_blocked(self):
        with self.assertRaises(ProviderUnavailable):
            select_provider(CODEX_PROVIDER_ID, {}, save=lambda s: None)

    def test_codex_ready_usable_for_chat_without_settings_select(self):
        sel = resolve_provider_selection({"provider_id": CODEX_PROVIDER_ID})
        self.assertEqual(sel.provider_id, CODEX_PROVIDER_ID)
        self.assertFalse(sel.fell_back)
        assert_provider_usable_for_chat(sel)

    def test_codex_unavailable_does_not_fall_back_to_local(self):
        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            reset_discovery_cache()
            sel = resolve_provider_selection({"provider_id": CODEX_PROVIDER_ID})
            self.assertEqual(sel.provider_id, CODEX_PROVIDER_ID)
            with self.assertRaises(ProviderUnavailable) as ctx:
                assert_provider_usable_for_chat(sel)
            self.assertEqual(ctx.exception.provider_id, CODEX_PROVIDER_ID)
            self.assertIn("disabled", (ctx.exception.message or "").lower())

    def test_not_signed_in_is_auth_error_not_local(self):
        bin_u = _make_fake_codex_bin(Path(self.tmp.name) / "u2", mode="ok")
        with mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(bin_u)}):
            reset_discovery_cache()
            reset_codex_session()
            ensure_codex_registered()
            sel = resolve_provider_selection({"provider_id": CODEX_PROVIDER_ID})
            with self.assertRaises(AuthenticationRequired) as ctx:
                assert_provider_usable_for_chat(sel)
            self.assertEqual(ctx.exception.provider_id, CODEX_PROVIDER_ID)

    def test_stream_routes_to_codex_only(self):
        local_calls = []
        openai_calls = []

        def boom_local(*a, **k):
            local_calls.append(1)
            raise AssertionError("local llama must not be called")

        class FakeBridge:
            @staticmethod
            def llama_post_stream(*a, **k):
                return boom_local()

        cancel_ev = threading.Event()
        payload = {
            "model": "codex",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        with mock.patch(
            "providers.inference_stream.OpenAIProvider.open_chat_stream",
            side_effect=lambda *a, **k: openai_calls.append(1) or (_ for _ in ()).throw(
                AssertionError("openai must not be called")
            ),
        ):
            handle, chunks = open_provider_chat_stream(
                provider_id=CODEX_PROVIDER_ID,
                payload=payload,
                cancel_ev=cancel_ev,
                bridge_module=FakeBridge,
                chat_id="chat-1",
            )
            text = b"".join(chunks).decode("utf-8")
        self.assertEqual(local_calls, [])
        self.assertEqual(openai_calls, [])
        self.assertIn("Hello", text)
        self.assertIn("Codex", text)
        self.assertIn("[DONE]", text)
        handle.close()

    def test_no_dual_dispatch(self):
        """Exactly one provider handles a submission — never local + Codex."""
        local_hits = []
        codex_hits = []
        payload = {
            "model": "codex",
            "messages": [{"role": "user", "content": "once"}],
            "stream": True,
        }

        class CountingCodex(CodexProvider):
            def stream_response(self, request, credentials=None):
                codex_hits.append(1)
                yield from super().stream_response(request, credentials)

        class FB:
            def llama_post_stream(self, *a, **k):
                local_hits.append(1)
                raise AssertionError("dual dispatch to local")

            def iter_llama_sse(self, *a, **k):
                local_hits.append(1)
                yield b""

            def _llama_gen_idle_timeout_s(self):
                return 1

            def _llama_gen_wall_timeout_s(self):
                return 1

            class _llama:
                _lock = threading.Lock()
                _proc = None

        with mock.patch("providers.inference_stream.CodexProvider", CountingCodex):
            handle, it = open_provider_chat_stream(
                provider_id=CODEX_PROVIDER_ID,
                payload=payload,
                cancel_ev=threading.Event(),
                bridge_module=FB(),
                chat_id="chat-dual",
            )
            list(it)
            handle.close()
        self.assertEqual(codex_hits, [1])
        self.assertEqual(local_hits, [])

    def test_codex_failure_does_not_open_local(self):
        local_hits = []

        class FB:
            def llama_post_stream(self, *a, **k):
                local_hits.append(1)
                raise AssertionError("must not fall back to local")

        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            reset_discovery_cache()
            with self.assertRaises(ProviderUnavailable) as ctx:
                open_provider_chat_stream(
                    provider_id=CODEX_PROVIDER_ID,
                    payload={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
                    cancel_ev=threading.Event(),
                    bridge_module=FB(),
                    chat_id="chat-fail",
                )
        self.assertEqual(ctx.exception.provider_id, CODEX_PROVIDER_ID)
        self.assertEqual(local_hits, [])

    def test_cancel_routed_to_codex(self):
        os.environ["FAKE_CODEX_TURN_DELAY_MS"] = "400"
        provider = CodexProvider()
        req = InferenceRequest(
            model="codex",
            messages=[{"role": "user", "content": "slow please"}],
            cancellation_id="chat-cancel",
            extra={"chat_id": "chat-cancel"},
        )
        events = []
        err = []

        def _run():
            try:
                for e in provider.stream_response(req):
                    events.append(e)
            except Exception as exc:
                err.append(exc)

        th = threading.Thread(target=_run)
        th.start()
        time.sleep(0.15)
        self.assertTrue(cancel_codex_for_chat("chat-cancel"))
        th.join(timeout=5)
        self.assertFalse(th.is_alive())
        kinds = [e.event_type for e in events]
        self.assertIn(InferenceEventType.COMPLETED, kinds)
        completed = [e for e in events if e.event_type == InferenceEventType.COMPLETED]
        self.assertEqual(completed[-1].completion_reason, "cancelled")

    def test_provider_stream_and_safe_metadata(self):
        provider = CodexProvider()
        req = InferenceRequest(
            model="fake-model",
            messages=[{"role": "user", "content": "hello"}],
            cancellation_id="chat-meta",
        )
        texts = []
        meta = None
        for evt in provider.stream_response(req):
            if evt.event_type == InferenceEventType.TEXT_DELTA and evt.text_delta:
                texts.append(evt.text_delta)
            if evt.event_type == InferenceEventType.COMPLETED:
                meta = evt.raw
        self.assertEqual("".join(texts), "Hello from Codex")
        safe = safe_codex_response_metadata(model_label="fake-model")
        self.assertEqual(safe["providerId"], CODEX_PROVIDER_ID)
        self.assertEqual(safe["providerDisplayName"], "ChatGPT / Codex")
        self.assertEqual(safe.get("modelLabel"), "fake-model")
        blob = json.dumps(safe)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("access_token", blob)
        if meta:
            assert_safe_provider_payload(meta)

    def test_status_exposes_inference_availability_not_selectable(self):
        st = get_codex_provider_status(live=True)
        self.assertFalse(st["supportsInference"])
        self.assertFalse(st["selectable"])
        avail = st.get("inferenceAvailability") or {}
        self.assertIn("status", avail)
        self.assertIn("ready", avail)
        assert_safe_provider_payload(st)


if __name__ == "__main__":
    unittest.main()

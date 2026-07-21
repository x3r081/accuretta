"""Session-bound inference provider — routing, persistence, cancel, no dual dispatch."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.inference_stream import open_provider_chat_stream
from providers.management import ensure_builtin_providers, resolve_chat_provider
from providers.registry import reset_default_registry
from providers.selection import DEFAULT_PROVIDER_ID
from providers.session_binding import (
    PROVIDER_ID_KEY,
    PROVIDER_LABEL_KEY,
    bind_inference_provider,
    ensure_session_provider,
    log_chat_dispatch,
    mismatch_notice,
    sanitize_provider_thread_state,
    settings_inference_provider_id,
)

CODEX = "codex_chatgpt"
LOCAL = DEFAULT_PROVIDER_ID


class SessionBindingUnitTest(unittest.TestCase):
    def test_new_session_inherits_settings_codex(self):
        chat = {"id": "n1", "messages": []}
        pid, label, notice = ensure_session_provider(
            chat, {"provider_id": CODEX}, for_new_chat=True
        )
        self.assertEqual(pid, CODEX)
        self.assertEqual(chat[PROVIDER_ID_KEY], CODEX)
        self.assertIn("Codex", label)
        self.assertIsNone(notice)

    def test_new_session_inherits_settings_local(self):
        chat = {"id": "n2", "messages": []}
        pid, label, notice = ensure_session_provider(
            chat, {"provider_id": LOCAL}, for_new_chat=True
        )
        self.assertEqual(pid, LOCAL)
        self.assertEqual(chat[PROVIDER_ID_KEY], LOCAL)
        self.assertIn("Local", label)
        self.assertIsNone(notice)

    def test_existing_session_keeps_provider_when_settings_change(self):
        chat = {"id": "e1", "messages": []}
        ensure_session_provider(chat, {"provider_id": LOCAL}, for_new_chat=True)
        pid, _, notice = ensure_session_provider(
            chat, {"provider_id": CODEX}, for_new_chat=False
        )
        self.assertEqual(pid, LOCAL)
        self.assertEqual(chat[PROVIDER_ID_KEY], LOCAL)
        self.assertIsNotNone(notice)
        self.assertIn("Local llama.cpp", notice)
        self.assertIn("Codex via ChatGPT", notice)
        self.assertIn("New sessions will use", notice)

    def test_settings_change_does_not_migrate_codex_session(self):
        chat = {"id": "e2", "messages": []}
        ensure_session_provider(chat, {"provider_id": CODEX}, for_new_chat=True)
        pid, _, notice = ensure_session_provider(
            chat, {"provider_id": LOCAL}, for_new_chat=False
        )
        self.assertEqual(pid, CODEX)
        self.assertIsNotNone(notice)
        self.assertIn("Codex via ChatGPT", notice)
        self.assertIn("Local llama.cpp", notice)

    def test_mismatch_notice_none_when_aligned(self):
        self.assertIsNone(mismatch_notice(LOCAL, LOCAL))
        self.assertIsNone(mismatch_notice(CODEX, CODEX))

    def test_legacy_infers_codex_from_thread_id(self):
        chat = {"id": "leg1", "codex_thread_id": "thr_abc", "messages": []}
        pid, _, _ = ensure_session_provider(
            chat, {"provider_id": LOCAL}, for_new_chat=False
        )
        self.assertEqual(pid, CODEX)

    def test_legacy_infers_from_message_provider_id(self):
        chat = {
            "id": "leg2",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "yo", "provider_id": CODEX},
            ],
        }
        pid, _, _ = ensure_session_provider(
            chat, {"provider_id": LOCAL}, for_new_chat=False
        )
        self.assertEqual(pid, CODEX)

    def test_sanitize_strips_codex_thread_from_local(self):
        chat = {
            "id": "s1",
            "codex_thread_id": "thr_should_go",
            "codex_cwd": "/tmp/ws",
        }
        bind_inference_provider(chat, LOCAL)
        self.assertNotIn("codex_thread_id", chat)
        sanitize_provider_thread_state(chat, LOCAL)
        self.assertNotIn("codex_thread_id", chat)

    def test_codex_session_may_keep_thread(self):
        chat = {"id": "s2", "codex_thread_id": "thr_keep"}
        bind_inference_provider(chat, CODEX)
        self.assertEqual(chat.get("codex_thread_id"), "thr_keep")

    def test_persist_survives_json_roundtrip(self):
        chat = {"id": "p1", "messages": []}
        ensure_session_provider(chat, {"provider_id": CODEX}, for_new_chat=True)
        blob = json.dumps({"chats": {"p1": chat}, "order": ["p1"]})
        restored = json.loads(blob)["chats"]["p1"]
        pid, label, notice = ensure_session_provider(
            restored, {"provider_id": LOCAL}, for_new_chat=False
        )
        self.assertEqual(pid, CODEX)
        self.assertEqual(restored[PROVIDER_ID_KEY], CODEX)
        self.assertEqual(restored[PROVIDER_LABEL_KEY], label)
        self.assertIsNotNone(notice)

    def test_dispatch_log_is_safe(self):
        logger = logging.getLogger("accuretta.provider.dispatch")
        with self.assertLogs(logger, level="INFO") as cm:
            log_chat_dispatch(
                session_id="sess-1",
                settings_provider_id=CODEX,
                dispatched_provider_id=LOCAL,
                turn_id="turn-9",
            )
        msg = "\n".join(cm.output)
        self.assertIn("session=sess-1", msg)
        self.assertIn("settings_provider=codex_chatgpt", msg)
        self.assertIn("dispatched=local_llama", msg)
        self.assertIn("turn=turn-9", msg)
        for bad in ("Authorization", "Bearer", "sk-", "password"):
            self.assertNotIn(bad.lower(), msg.lower())

    def test_dispatch_log_emits_to_stderr_for_bridge_operators(self):
        """Regression: unconfigured root logger must not hide dispatch evidence."""
        from io import StringIO
        from contextlib import redirect_stderr

        buf = StringIO()
        with redirect_stderr(buf):
            log_chat_dispatch(
                session_id="sess-stderr",
                settings_provider_id=LOCAL,
                dispatched_provider_id=CODEX,
                turn_id="turn-stderr",
            )
        out = buf.getvalue()
        self.assertIn("[provider] chat_dispatch", out)
        self.assertIn("session=sess-stderr", out)
        self.assertIn("dispatched=codex_chatgpt", out)
        self.assertIn("settings_provider=local_llama", out)
        self.assertNotIn("Authorization", out)
        self.assertNotIn("Bearer", out)


class SessionDispatchBoundaryTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        ensure_builtin_providers()

    def tearDown(self):
        reset_default_registry()

    def test_resolve_chat_provider_honors_explicit_session_id(self):
        settings = {"provider_id": CODEX}
        sel = resolve_chat_provider(settings, provider_id=LOCAL)
        self.assertEqual(sel.provider_id, LOCAL)
        self.assertFalse(getattr(sel, "fell_back", False))

    def test_new_codex_settings_dispatch_path(self):
        """Selecting Codex then creating a session → dispatch resolves to Codex."""
        settings = {"provider_id": CODEX}
        chat = {"id": "d1", "messages": []}
        pid, _, _ = ensure_session_provider(chat, settings, for_new_chat=True)
        self.assertEqual(pid, CODEX)
        with mock.patch(
            "providers.management.assert_provider_usable_for_chat",
            return_value=None,
        ):
            sel = resolve_chat_provider(settings, provider_id=pid)
        self.assertEqual(sel.provider_id, CODEX)

    def test_new_local_settings_dispatch_path(self):
        settings = {"provider_id": LOCAL, "model": "qwen-test"}
        chat = {"id": "d2", "messages": []}
        pid, _, _ = ensure_session_provider(chat, settings, for_new_chat=True)
        self.assertEqual(pid, LOCAL)
        sel = resolve_chat_provider(settings, provider_id=pid)
        self.assertEqual(sel.provider_id, LOCAL)

    def test_no_silent_fallback_when_resolving_codex_session(self):
        settings = {"provider_id": LOCAL}
        chat = {"id": "d3", "messages": []}
        ensure_session_provider(chat, {"provider_id": CODEX}, for_new_chat=True)
        # Settings are local but session is Codex — resolve must keep Codex id
        # (usability assert may raise; must not remap to local).
        from providers.selection import resolve_provider_selection

        selection = resolve_provider_selection(settings, requested_id=chat[PROVIDER_ID_KEY])
        self.assertEqual(selection.provider_id, CODEX)
        self.assertFalse(selection.fell_back)

    def test_open_stream_local_never_calls_codex(self):
        codex_hits = []
        local_hits = []

        class FB:
            def llama_post_stream(self, *a, **k):
                local_hits.append(1)

                class R:
                    def close(self):
                        pass

                    def read(self, n):
                        return b""

                return R()

            def iter_llama_sse(self, *a, **k):
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                yield b"data: [DONE]\n\n"

            def _llama_gen_idle_timeout_s(self):
                return 1

            def _llama_gen_wall_timeout_s(self):
                return 1

        with mock.patch(
            "providers.inference_stream.CodexProvider.stream_response",
            side_effect=lambda *a, **k: codex_hits.append(1) or (_ for _ in ()).throw(
                AssertionError("codex must not run")
            ),
        ):
            handle, it = open_provider_chat_stream(
                provider_id=LOCAL,
                payload={"model": "m", "messages": [{"role": "user", "content": "x"}]},
                cancel_ev=threading.Event(),
                bridge_module=FB(),
                chat_id="local-only",
            )
            list(it)
            handle.close()
        self.assertEqual(local_hits, [1])
        self.assertEqual(codex_hits, [])

    def test_local_session_does_not_pass_codex_thread(self):
        """Dispatch for local must not bind / reuse a Codex thread id."""
        seen = {}

        def capture_open(**kwargs):
            seen.update(kwargs)
            class R:
                def close(self):
                    pass

            def _iter():
                yield b"data: [DONE]\n\n"

            return R(), _iter()

        with mock.patch(
            "providers.inference_stream.open_provider_chat_stream",
            side_effect=lambda **kw: capture_open(**kw),
        ):
            # Simulate the bridge call site: thread_id only when Codex.
            provider_id = LOCAL
            thread_id = "thr_should_not" if provider_id == CODEX else None
            open_provider_chat_stream(
                provider_id=provider_id,
                payload={"model": "m", "messages": []},
                cancel_ev=threading.Event(),
                chat_id="loc",
                thread_id=thread_id,
            )
        self.assertIsNone(seen.get("thread_id"))


class CancelTargetsActiveProviderTest(unittest.TestCase):
    def test_cancel_codex_session_calls_codex_only(self):
        import bridge

        chats = {
            "chats": {
                "cx1": {
                    "id": "cx1",
                    PROVIDER_ID_KEY: CODEX,
                    PROVIDER_LABEL_KEY: "Codex via ChatGPT",
                }
            },
            "order": ["cx1"],
        }
        codex_calls = []
        with mock.patch.object(bridge, "get_chats", return_value=chats):
            with mock.patch(
                "providers.codex_provider.cancel_codex_for_chat",
                side_effect=lambda cid: codex_calls.append(cid) or True,
            ):
                with bridge._chat_cancels_lock:
                    bridge._chat_cancels.clear()
                ok = bridge.cancel_chat("cx1")
        self.assertTrue(ok)
        self.assertEqual(codex_calls, ["cx1"])

    def test_cancel_local_session_does_not_call_codex(self):
        import bridge

        chats = {
            "chats": {
                "lx1": {
                    "id": "lx1",
                    PROVIDER_ID_KEY: LOCAL,
                    PROVIDER_LABEL_KEY: "Local llama.cpp",
                }
            },
            "order": ["lx1"],
        }
        codex_calls = []
        ev = threading.Event()

        class FakeResp:
            def close(self):
                pass

        with mock.patch.object(bridge, "get_chats", return_value=chats):
            with mock.patch(
                "providers.codex_provider.cancel_codex_for_chat",
                side_effect=lambda cid: codex_calls.append(cid) or True,
            ):
                with bridge._chat_cancels_lock:
                    bridge._chat_cancels["lx1"] = {"cancel": ev, "resp": FakeResp()}
                ok = bridge.cancel_chat("lx1")
                with bridge._chat_cancels_lock:
                    bridge._chat_cancels.pop("lx1", None)
        self.assertTrue(ok)
        self.assertTrue(ev.is_set())
        self.assertEqual(codex_calls, [])


class PersistCodexThreadGuardTest(unittest.TestCase):
    def test_local_bound_chat_drops_thread_on_sanitize(self):
        chat = {
            "id": "mix",
            PROVIDER_ID_KEY: LOCAL,
            "codex_thread_id": "thr_x",
            "messages": [],
        }
        ensure_session_provider(chat, {"provider_id": LOCAL}, for_new_chat=False)
        self.assertNotIn("codex_thread_id", chat)

    def test_settings_provider_id_helper(self):
        self.assertEqual(
            settings_inference_provider_id({"provider_id": CODEX}), CODEX
        )
        self.assertEqual(settings_inference_provider_id({}), LOCAL)


if __name__ == "__main__":
    unittest.main()

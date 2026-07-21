"""Codex write/shell approval policy tests."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex.approvals import (
    DEFAULT_WRITE_MODE,
    WRITE_MODE_ASK,
    WRITE_MODE_CHAT_ONLY,
    WRITE_MODE_WORKSPACE_AUTO,
    decide_command_execution,
    decide_file_change,
    get_codex_write_mode,
    normalize_codex_write_mode,
    path_under_workspace,
    sandbox_for_write_mode,
)
from codex.discover import reset_discovery_cache
from codex.flags import ENV_CODEX_INFERENCE_ENABLED
from codex.protocol import build_thread_start_params
from codex.session import reset_codex_session
from providers.base import InferenceRequest
from providers.codex_provider import (
    CodexProvider,
    clear_codex_thread_for_chat,
    get_bound_codex_thread,
)
from providers.codex_workspace import (
    clear_codex_cwd_for_chat,
    resolve_codex_workspace,
    validate_codex_cwd,
)
from providers.management import ensure_builtin_providers, reset_auth_store_info
from providers.registry import reset_default_registry

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


class CodexWriteModeUnitTest(unittest.TestCase):
    def test_default_is_ask(self):
        self.assertEqual(DEFAULT_WRITE_MODE, WRITE_MODE_ASK)
        self.assertEqual(normalize_codex_write_mode(None), WRITE_MODE_ASK)
        self.assertEqual(normalize_codex_write_mode("advisory_chat_only"), WRITE_MODE_CHAT_ONLY)

    def test_sandbox_mapping(self):
        self.assertEqual(sandbox_for_write_mode(WRITE_MODE_CHAT_ONLY), "read-only")
        self.assertEqual(sandbox_for_write_mode(WRITE_MODE_ASK), "workspace-write")
        self.assertEqual(sandbox_for_write_mode(WRITE_MODE_WORKSPACE_AUTO), "workspace-write")

    def test_danger_full_access_never_allowed(self):
        params = build_thread_start_params(sandbox="danger-full-access")
        self.assertEqual(params["sandbox"], "workspace-write")

    def test_workspace_write_allowed_when_requested(self):
        params = build_thread_start_params(sandbox="workspace-write")
        self.assertEqual(params["sandbox"], "workspace-write")

    def test_chat_only_declines_file_and_shell(self):
        ws = "/tmp/ws"
        self.assertEqual(
            decide_file_change(
                mode=WRITE_MODE_CHAT_ONLY,
                params={"grantRoot": ws},
                workspace_cwd=ws,
            )["decision"],
            "decline",
        )
        self.assertEqual(
            decide_command_execution(
                mode=WRITE_MODE_CHAT_ONLY,
                params={"command": "ls", "cwd": ws},
                workspace_cwd=ws,
            )["decision"],
            "decline",
        )

    def test_outside_workspace_always_declined(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = str(Path(tmp) / "proj")
            Path(ws).mkdir()
            outside = str(Path(tmp) / "other" / "x.txt")
            Path(tmp, "other").mkdir()
            Path(outside).write_text("x", encoding="utf-8")
            self.assertFalse(path_under_workspace(outside, ws))
            for mode in (WRITE_MODE_ASK, WRITE_MODE_WORKSPACE_AUTO):
                self.assertEqual(
                    decide_file_change(
                        mode=mode,
                        params={"paths": [outside]},
                        workspace_cwd=ws,
                        request_approval=lambda *a, **k: {"decision": "approve"},
                    )["decision"],
                    "decline",
                )

    def test_ask_prompts_and_honors_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = str(Path(tmp) / "proj")
            Path(ws).mkdir()
            target = str(Path(ws) / "out.txt")
            calls = []

            def req(title, command, details=None, timeout_s=600):
                calls.append((title, command, details))
                return {"decision": "approve"}

            out = decide_file_change(
                mode=WRITE_MODE_ASK,
                params={"paths": [target]},
                workspace_cwd=ws,
                request_approval=req,
            )
            self.assertEqual(out["decision"], "accept")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2]["kind"], "codex_file_change")

            out_deny = decide_file_change(
                mode=WRITE_MODE_ASK,
                params={"paths": [target]},
                workspace_cwd=ws,
                request_approval=lambda *a, **k: {"decision": "deny"},
            )
            self.assertEqual(out_deny["decision"], "decline")

    def test_workspace_auto_accepts_inside_without_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = str(Path(tmp) / "proj")
            Path(ws).mkdir()
            target = str(Path(ws) / "auto.txt")

            def boom(*a, **k):
                raise AssertionError("should not prompt in workspace_auto")

            out = decide_file_change(
                mode=WRITE_MODE_WORKSPACE_AUTO,
                params={"paths": [target]},
                workspace_cwd=ws,
                request_approval=boom,
            )
            self.assertEqual(out["decision"], "accept")

    def test_shell_still_requires_approval_in_workspace_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = str(Path(tmp) / "proj")
            Path(ws).mkdir()
            calls = []

            def req(title, command, details=None, timeout_s=600):
                calls.append(details)
                return {"decision": "approve"}

            out = decide_command_execution(
                mode=WRITE_MODE_WORKSPACE_AUTO,
                params={"command": "echo hi", "cwd": ws},
                workspace_cwd=ws,
                request_approval=req,
            )
            self.assertEqual(out["decision"], "accept")
            self.assertEqual(calls[0]["kind"], "codex_shell")

    def test_demo_approved_file_creation(self):
        """Demonstrate: approved in-workspace write creates the file."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "proj"
            ws.mkdir()
            target = ws / "created_by_codex.txt"
            decision = decide_file_change(
                mode=WRITE_MODE_ASK,
                params={"paths": [str(target)]},
                workspace_cwd=str(ws),
                request_approval=lambda *a, **k: {"decision": "approve"},
            )
            self.assertEqual(decision["decision"], "accept")
            # Simulate Codex applying the change after Accuretta accepts.
            target.write_text("hello from approved Codex write\n", encoding="utf-8")
            self.assertTrue(target.is_file())
            self.assertIn("approved Codex write", target.read_text(encoding="utf-8"))


class CodexWriteModeIntegrationTest(unittest.TestCase):
    def setUp(self):
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "proj"
        self.ws.mkdir()
        self.write_path = self.ws / "demo.txt"
        self.bin = _make_fake_codex_bin(Path(self.tmp.name), mode="authenticated")
        self._env = mock.patch.dict(
            os.environ,
            {
                "ACCURETTA_CODEX_BIN": str(self.bin),
                ENV_CODEX_INFERENCE_ENABLED: "1",
                "FAKE_CODEX_TURN_DELAY_MS": "15",
                "FAKE_CODEX_REQUEST_WRITE": "1",
                "FAKE_CODEX_WRITE_PATH": str(self.write_path),
            },
            clear=False,
        )
        self._env.start()
        ensure_builtin_providers()
        clear_codex_thread_for_chat("chat-write")
        clear_codex_cwd_for_chat("chat-write")

    def tearDown(self):
        clear_codex_thread_for_chat("chat-write")
        clear_codex_cwd_for_chat("chat-write")
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

    def test_end_to_end_approved_write_creates_file(self):
        settings = {"codex_write_mode": WRITE_MODE_ASK, "provider_id": "codex_chatgpt"}

        def auto_approve(title, command, details=None, timeout_s=600):
            return {"status": "auto-approved", "decision": "approve", "details": details or {}}

        with mock.patch(
            "providers.codex_workspace._accuretta_workspace_folders",
            return_value=[str(self.ws.resolve())],
        ), mock.patch(
            "codex.inference.get_codex_write_mode",
            return_value=WRITE_MODE_ASK,
        ), mock.patch(
            "codex.approvals.get_codex_write_mode",
            return_value=WRITE_MODE_ASK,
        ), mock.patch(
            "bridge.request_approval",
            side_effect=auto_approve,
        ), mock.patch(
            "bridge.get_settings",
            return_value=settings,
        ):
            provider = CodexProvider()
            chunks = list(
                provider.stream_response(
                    InferenceRequest(
                        model="codex",
                        messages=[{"role": "user", "content": "create demo.txt"}],
                        cancellation_id="chat-write",
                        extra={"chat_id": "chat-write"},
                    )
                )
            )
        self.assertTrue(any(c.event_type.value == "completed" or getattr(c, "completion_reason", None) for c in chunks) or chunks)
        self.assertTrue(self.write_path.is_file(), f"expected file at {self.write_path}; chunks={chunks!r}")
        self.assertIn("approved Codex write", self.write_path.read_text(encoding="utf-8"))
        self.assertTrue(get_bound_codex_thread("chat-write"))

    def test_chat_only_does_not_create_file(self):
        with mock.patch(
            "providers.codex_workspace._accuretta_workspace_folders",
            return_value=[str(self.ws.resolve())],
        ), mock.patch(
            "codex.inference.get_codex_write_mode",
            return_value=WRITE_MODE_CHAT_ONLY,
        ), mock.patch(
            "codex.approvals.get_codex_write_mode",
            return_value=WRITE_MODE_CHAT_ONLY,
        ), mock.patch(
            "bridge.get_settings",
            return_value={"codex_write_mode": WRITE_MODE_CHAT_ONLY},
        ):
            provider = CodexProvider()
            list(
                provider.stream_response(
                    InferenceRequest(
                        model="codex",
                        messages=[{"role": "user", "content": "create demo.txt"}],
                        cancellation_id="chat-write",
                        extra={"chat_id": "chat-write"},
                    )
                )
            )
        self.assertFalse(self.write_path.exists())


class CodexWorkspaceModeContractTest(unittest.TestCase):
    def test_binding_reflects_settings_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "proj"
            ws.mkdir()
            with mock.patch(
                "providers.codex_workspace.get_codex_write_mode",
                return_value=WRITE_MODE_ASK,
            ):
                r = validate_codex_cwd(str(ws), allowed_roots=[str(ws)])
            self.assertTrue(r.coding_actions_allowed)
            self.assertEqual(r.mode, WRITE_MODE_ASK)

    def test_ui_exposes_write_mode_control(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="set-codex-write-mode"', html)
        self.assertIn('value="ask"', html)
        js = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("codex_write_mode", js)
        self.assertIn("codex_file_change", js)

    def test_settings_default_is_ask(self):
        import bridge
        self.assertEqual(bridge.DEFAULT_SETTINGS.get("codex_write_mode"), "ask")
        self.assertEqual(get_codex_write_mode({"codex_write_mode": "ask"}), "ask")

    def test_docs_describe_modes(self):
        doc = (ROOT / "docs" / "codex-workspace-security.md").read_text(encoding="utf-8")
        self.assertIn("codex_write_mode", doc)
        self.assertIn("Ask before every write", doc)
        self.assertIn("workspace-write", doc)
        self.assertIn("danger-full-access", doc.lower())


if __name__ == "__main__":
    unittest.main()

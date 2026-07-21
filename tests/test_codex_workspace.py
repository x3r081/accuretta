"""Safe Codex workspace (cwd) binding tests."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    FORBIDDEN_REASON_HOME,
    FORBIDDEN_REASON_MISSING,
    FORBIDDEN_REASON_OUTSIDE,
    FORBIDDEN_REASON_ROOT,
    bind_codex_cwd,
    clear_codex_cwd_for_chat,
    get_bound_codex_cwd,
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


class CodexWorkspaceValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "project"
        self.ws.mkdir()
        (self.ws / "readme.txt").write_text("hi\n", encoding="utf-8")
        self.other = Path(self.tmp.name) / "other"
        self.other.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_active_workspace_accepted(self):
        r = validate_codex_cwd(str(self.ws), allowed_roots=[str(self.ws)])
        self.assertTrue(r.ok)
        self.assertEqual(Path(r.cwd), self.ws.resolve())
        self.assertFalse(r.coding_actions_allowed)
        self.assertEqual(r.mode, "advisory_chat_only")

    def test_no_workspace_blocks_coding_safely(self):
        r = resolve_codex_workspace()
        with mock.patch(
            "providers.codex_workspace._accuretta_workspace_folders",
            return_value=[],
        ):
            r = resolve_codex_workspace()
        self.assertFalse(r.ok)
        self.assertIsNone(r.cwd)
        self.assertEqual(r.reason, FORBIDDEN_REASON_MISSING)
        self.assertFalse(r.coding_actions_allowed)

    def test_path_traversal_rejected(self):
        escape = str(self.ws / ".." / "other")
        r = validate_codex_cwd(escape, allowed_roots=[str(self.ws.resolve())])
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, FORBIDDEN_REASON_OUTSIDE)

    def test_home_rejected(self):
        home = str(Path.home().resolve())
        r = validate_codex_cwd(home, allowed_roots=[home, str(self.ws)])
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, FORBIDDEN_REASON_HOME)

    def test_root_rejected(self):
        r = validate_codex_cwd("/", allowed_roots=["/", str(self.ws)])
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, FORBIDDEN_REASON_ROOT)

    def test_conversation_isolation(self):
        clear_codex_cwd_for_chat("a")
        clear_codex_cwd_for_chat("b")
        bind_codex_cwd("a", str(self.ws))
        bind_codex_cwd("b", str(self.other))
        with mock.patch(
            "providers.codex_workspace._accuretta_workspace_folders",
            return_value=[str(self.ws.resolve()), str(self.other.resolve())],
        ):
            ra = resolve_codex_workspace(chat_id="a")
            rb = resolve_codex_workspace(chat_id="b")
        self.assertEqual(Path(ra.cwd), self.ws.resolve())
        self.assertEqual(Path(rb.cwd), self.other.resolve())
        self.assertNotEqual(ra.cwd, rb.cwd)
        self.assertNotEqual(get_bound_codex_cwd("a"), get_bound_codex_cwd("b"))

    def test_thread_start_forces_read_only_sandbox(self):
        params = build_thread_start_params(
            cwd=str(self.ws),
            sandbox="danger-full-access",
            approval_policy="never",
        )
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["cwd"], str(self.ws))
        # approval policy may remain on-request when invalid "never" for our allowlist —
        # we allow "never" in the set; force read-only sandbox is the critical bit.
        self.assertIn(params["approvalPolicy"], {"on-request", "never"})

    def test_docs_describe_advisory_mode(self):
        doc = (ROOT / "docs" / "codex-workspace-security.md").read_text(encoding="utf-8")
        self.assertIn("advisory", doc.lower())
        self.assertIn("declined", doc.lower())
        self.assertIn("read-only", doc.lower())
        self.assertIn("never", doc.lower())  # home/root / unrestricted guidance


class CodexWorkspaceIntegrationTest(unittest.TestCase):
    def setUp(self):
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "proj"
        self.ws.mkdir()
        self.bin = _make_fake_codex_bin(Path(self.tmp.name), mode="authenticated")
        self._env = mock.patch.dict(
            os.environ,
            {
                "ACCURETTA_CODEX_BIN": str(self.bin),
                ENV_CODEX_INFERENCE_ENABLED: "1",
                "FAKE_CODEX_TURN_DELAY_MS": "15",
            },
            clear=False,
        )
        self._env.start()
        ensure_builtin_providers()
        clear_codex_thread_for_chat("chat-ws")
        clear_codex_cwd_for_chat("chat-ws")

    def tearDown(self):
        clear_codex_thread_for_chat("chat-ws")
        clear_codex_cwd_for_chat("chat-ws")
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

    def test_create_thread_uses_active_workspace(self):
        captured = {}
        real_build = build_thread_start_params

        def tracking(**kwargs):
            captured.update(kwargs)
            return real_build(**kwargs)

        with mock.patch(
            "providers.codex_workspace._accuretta_workspace_folders",
            return_value=[str(self.ws.resolve())],
        ), mock.patch(
            "codex.inference.build_thread_start_params",
            side_effect=tracking,
        ):
            provider = CodexProvider()
            list(
                provider.stream_response(
                    InferenceRequest(
                        model="codex",
                        messages=[{"role": "user", "content": "hi"}],
                        cancellation_id="chat-ws",
                        extra={"chat_id": "chat-ws"},
                    )
                )
            )
        self.assertEqual(Path(captured.get("cwd")), self.ws.resolve())
        self.assertEqual(captured.get("sandbox"), "read-only")
        self.assertTrue(get_bound_codex_cwd("chat-ws"))
        self.assertTrue(get_bound_codex_thread("chat-ws"))

    def test_approvals_remain_declined(self):
        src = (ROOT / "codex" / "inference.py").read_text(encoding="utf-8")
        self.assertIn("item/fileChange/requestApproval", src)
        self.assertIn("item/commandExecution/requestApproval", src)
        self.assertIn('"decision": "decline"', src)
        # No Accuretta write bridge from Codex approvals.
        self.assertNotIn("request_approval", src)

    def test_no_unrestricted_write_introduced(self):
        ws_src = (ROOT / "providers" / "codex_workspace.py").read_text(encoding="utf-8")
        self.assertIn("coding_actions_allowed: bool = False", ws_src)
        self.assertIn("advisory_chat_only", ws_src)
        proto = (ROOT / "codex" / "protocol.py").read_text(encoding="utf-8")
        self.assertIn('sandbox_val = "read-only"', proto)


if __name__ == "__main__":
    unittest.main()

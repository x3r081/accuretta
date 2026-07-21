"""Codex ChatGPT authentication tests (fake subprocess — no network)."""

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

import bridge
from auth.file_store import FileAuthStore
from auth.store import AuthStoreInfo
from codex.discover import discover_codex, reset_discovery_cache
from codex.process import CodexAppServerProcess
from codex.protocol import parse_account_read_result, validate_auth_url
from codex.rpc_client import CodexRpcClient
from codex.session import CodexSession, reset_codex_session, shutdown_codex
from providers.codex_provider import (
    CODEX_PROVIDER_ID,
    codex_cancel_login,
    codex_connect_browser,
    codex_login_status,
    codex_logout,
    codex_start_device,
    ensure_codex_registered,
    get_codex_provider_status,
)
from providers.management import (
    list_provider_statuses,
    reset_auth_store_info,
    select_provider,
    shutdown_provider_background,
)
from providers.errors import ProviderUnavailable
from providers.openai_provider import FAKE_KEY_MARKER, api_key_credential
from providers.registry import reset_default_registry
from providers.status import assert_safe_provider_payload

FAKE_SERVER = Path(__file__).resolve().parent / "fake_codex_app_server.py"
SECRET = "sk-fake-SECRET-marker-do-not-leak"


def _make_fake_codex_bin(tmp: Path, *, mode: str = "ok") -> Path:
    """Create an executable wrapper that runs the fake server with python."""
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


class CodexDiscoveryTest(unittest.TestCase):
    def setUp(self):
        reset_discovery_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        reset_discovery_cache()
        self.tmp.cleanup()

    def test_missing_executable(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACCURETTA_CODEX_BIN", None)
            with mock.patch("codex.discover.shutil.which", return_value=None):
                with mock.patch("codex.discover._candidate_paths", return_value=[]):
                    d = discover_codex(force_refresh=True)
        self.assertFalse(d.available)
        self.assertIn("not installed", (d.disabled_reason or "").lower())

    def test_explicit_path(self):
        bin_path = _make_fake_codex_bin(self.root)
        with mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(bin_path)}):
            d = discover_codex(force_refresh=True)
        self.assertTrue(d.available)
        self.assertEqual(d.source, "env")

    def test_non_executable_path(self):
        bad = self.root / "not-exec"
        bad.write_text("x", encoding="utf-8")
        bad.chmod(0o644)
        with mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(bad)}):
            d = discover_codex(force_refresh=True)
        self.assertFalse(d.available)
        self.assertEqual(d.source, "invalid")

    def test_path_discovery(self):
        bin_path = _make_fake_codex_bin(self.root)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACCURETTA_CODEX_BIN", None)
            with mock.patch("codex.discover.shutil.which", return_value=str(bin_path)):
                d = discover_codex(force_refresh=True, candidates=[])
        self.assertTrue(d.available)

    def test_homebrew_fallback(self):
        bin_path = _make_fake_codex_bin(self.root)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACCURETTA_CODEX_BIN", None)
            with mock.patch("codex.discover.shutil.which", return_value=None):
                with mock.patch(
                    "codex.discover._candidate_paths",
                    return_value=["/opt/homebrew/bin/codex"],
                ):
                    with mock.patch(
                        "codex.discover._validate_executable",
                        return_value=str(bin_path),
                    ):
                        with mock.patch(
                            "codex.discover._probe_version",
                            return_value="0.144.6",
                        ):
                            d = discover_codex(force_refresh=True)
        self.assertTrue(d.available)
        self.assertEqual(d.source, "homebrew")
        self.assertEqual(d.version, "0.144.6")


class CodexProcessRpcTest(unittest.TestCase):
    def setUp(self):
        reset_codex_session()
        reset_discovery_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = _make_fake_codex_bin(Path(self.tmp.name))

    def tearDown(self):
        shutdown_codex()
        self.tmp.cleanup()

    def test_start_initialize_and_account_read(self):
        proc = CodexAppServerProcess(str(self.bin))
        proc.start()
        self.assertTrue(proc.ready)
        result = proc.rpc.request("account/read", {"refreshToken": False})
        view = parse_account_read_result(result)
        self.assertFalse(view.authenticated)
        # Poison fields discarded
        blob = json.dumps(view.to_dict())
        self.assertNotIn(SECRET, blob)
        proc.terminate()
        self.assertFalse(proc.ready)

    def test_browser_and_device_login(self):
        session = CodexSession(executable=str(self.bin))
        pending = session.start_browser_login()
        self.assertEqual(pending["loginMethod"], "browser")
        self.assertIn("loginId", pending)
        self.assertTrue(str(pending.get("authUrl", "")).startswith("https://"))
        self.assertNotIn(SECRET, json.dumps(pending))
        # Wait for auto-complete
        deadline = time.time() + 3
        while time.time() < deadline:
            st = session.login_status()
            if st.get("authenticated") or st.get("loginStatus") == "completed":
                break
            time.sleep(0.05)
        st = get_codex_provider_status.__wrapped__ if False else session.status_dto(live=True)
        self.assertTrue(st.get("authenticated"))
        self.assertEqual(st.get("planType"), "pro")
        self.assertNotIn(SECRET, json.dumps(st))
        session.logout()
        st2 = session.status_dto(live=True)
        self.assertFalse(st2.get("authenticated"))

        pending2 = session.start_device_login()
        self.assertEqual(pending2.get("userCode"), "ABCD-EFGH")
        self.assertNotIn("device_code", json.dumps(pending2))
        self.assertNotIn(SECRET, json.dumps(pending2))
        session.cancel_login(pending2.get("loginId"))
        session.shutdown()

    def test_no_shell_true(self):
        # Inspect process.py source
        src = (Path(__file__).resolve().parent.parent / "codex" / "process.py").read_text()
        self.assertIn("Popen(cmd, env=env, shell=False", src)

    def test_malformed_stdout_ignored(self):
        client = CodexRpcClient(write_line=lambda s: None)
        client.feed_line("not-json")
        client.feed_line("")
        client.close()

    def test_auth_url_strips_secrets(self):
        url = validate_auth_url(
            f"https://chatgpt.com/x?redirect_uri=http://localhost&access_token={SECRET}"
        )
        self.assertIsNotNone(url)
        self.assertNotIn(SECRET, url or "")
        self.assertNotIn("access_token", url or "")

    def test_rpc_out_of_order_correlation(self):
        client = CodexRpcClient(write_line=lambda s: None)
        fut_holder = {}

        def start_one(name):
            fut_holder[name] = client.request(name, {"n": name}, timeout=2.0)

        th1 = threading.Thread(target=start_one, args=("one",))
        th2 = threading.Thread(target=start_one, args=("two",))
        th1.start()
        time.sleep(0.02)
        th2.start()
        time.sleep(0.05)
        # Responses: id 2 then id 1
        client.feed_line(json.dumps({"id": 2, "result": {"ok": 2}}))
        client.feed_line(json.dumps({"id": 1, "result": {"ok": 1}}))
        th1.join(timeout=2)
        th2.join(timeout=2)
        self.assertEqual(fut_holder["one"], {"ok": 1})
        self.assertEqual(fut_holder["two"], {"ok": 2})
        client.close()

    def test_stale_login_ignored(self):
        session = CodexSession(executable=str(self.bin))
        os.environ["FAKE_CODEX_AUTO_COMPLETE"] = "0"
        try:
            p1 = session.start_browser_login()
            p2 = session.start_device_login()
            self.assertNotEqual(p1["loginId"], p2["loginId"])
            # Stale cancel must not cancel the newer login
            out = session.cancel_login(p1["loginId"])
            self.assertFalse(out.get("cancelled"))
            self.assertEqual(out.get("reason"), "stale_login_id")
            session.cancel_login(p2["loginId"])
        finally:
            os.environ.pop("FAKE_CODEX_AUTO_COMPLETE", None)
            session.shutdown()

    def test_unknown_notification_ignored(self):
        seen = []
        client = CodexRpcClient(
            write_line=lambda s: None,
            on_notification=lambda m, p: seen.append(m),
        )
        client.feed_line(json.dumps({"method": "thread/started", "params": {"id": "x"}}))
        client.feed_line(json.dumps({"method": "account/updated", "params": {"authMode": "chatgpt"}}))
        self.assertIn("thread/started", seen)
        self.assertIn("account/updated", seen)
        client.close()


class CodexApiBoundaryTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        reset_codex_session()
        reset_discovery_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = _make_fake_codex_bin(Path(self.tmp.name))
        self.store = FileAuthStore(path=Path(self.tmp.name) / "c.json")
        from providers import management as mgmt
        mgmt._auth_info = AuthStoreInfo(
            store=self.store, backend="file",
            secure_cloud_auth_available=True, detail="test",
        )
        self.store.save("openai", api_key_credential(FAKE_KEY_MARKER, validated=True))
        self._env = mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(self.bin)})
        self._env.start()
        ensure_codex_registered()
        # Force session to use our bin
        from codex import session as sess
        sess._SESSION = CodexSession(executable=str(self.bin))

    def tearDown(self):
        shutdown_provider_background()
        self._env.stop()
        reset_codex_session()
        reset_auth_store_info()
        reset_default_registry()
        reset_discovery_cache()
        self.tmp.cleanup()

    def test_status_safe(self):
        st = get_codex_provider_status(live=True)
        assert_safe_provider_payload(st)
        self.assertFalse(st["supportsInference"])
        self.assertFalse(st["selectable"])
        blob = json.dumps(st)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("access_token", blob)

    def test_not_selectable(self):
        with self.assertRaises(ProviderUnavailable):
            select_provider(CODEX_PROVIDER_ID, {}, save=lambda s: None)

    def test_authstore_untouched_by_login(self):
        saves = []
        orig = self.store.save

        def tracked(pid, cred):
            saves.append(pid)
            return orig(pid, cred)

        self.store.save = tracked  # type: ignore
        codex_connect_browser()
        time.sleep(0.3)
        codex_login_status()
        self.assertNotIn(CODEX_PROVIDER_ID, saves)
        # OpenAI credential remains
        self.assertEqual(self.store.load("openai").access_token, FAKE_KEY_MARKER)
        codex_logout()
        self.assertIsNotNone(self.store.load("openai"))

    def test_device_start_safe(self):
        os.environ["FAKE_CODEX_AUTO_COMPLETE"] = "0"
        try:
            out = codex_start_device()
        finally:
            os.environ.pop("FAKE_CODEX_AUTO_COMPLETE", None)
        assert_safe_provider_payload(out)
        self.assertEqual(out.get("userCode"), "ABCD-EFGH")
        self.assertNotIn(SECRET, json.dumps(out))
        cancel = codex_cancel_login({"loginId": out.get("loginId")})
        self.assertTrue(cancel.get("cancelled") or cancel.get("ok"))

    def test_list_providers_includes_codex_without_breaking_local(self):
        body = list_provider_statuses({"provider_id": "local_llama"})
        assert_safe_provider_payload(body)
        ids = {p["providerId"] for p in body["providers"]}
        self.assertIn("local_llama", ids)
        self.assertIn(CODEX_PROVIDER_ID, ids)
        self.assertEqual(body["selectedProviderId"], "local_llama")
        self.assertIn("codexInstalled", body["diagnostics"])

    def test_never_reads_codex_auth_json(self):
        # Grep source guarantee — no filesystem reads of Codex credential files.
        root = Path(__file__).resolve().parent.parent
        for path in (root / "codex").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("~/.codex/auth.json", text)
            self.assertNotIn('".codex/auth.json"', text)
            self.assertNotIn("Path.home() / \".codex\"", text)

    def test_ui_contract(self):
        html = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="codex-auth-section"', html)
        self.assertIn("Sign in with ChatGPT", html)
        self.assertIn("Use device code", html)
        self.assertIn("populateCodexAuthForm", js)
        self.assertIn("/api/providers/codex_chatgpt/", js)
        self.assertNotIn("localStorage", js[js.find("populateCodexAuthForm"):js.find("function _clearGitHubDeviceUi")])
        self.assertNotIn("sessionStorage", js[js.find("populateCodexAuthForm"):js.find("function _clearGitHubDeviceUi")])
        self.assertNotIn("access_token", js)
        # Inference selector still skips supportsInference === false
        self.assertIn("supportsInference === false", js)

    def test_shutdown_terminates_process(self):
        session = CodexSession(executable=str(self.bin))
        session.ensure_ready()
        self.assertEqual(session.process_state(), "ready")
        shutdown_provider_background()
        # New singleton has no process
        from codex.session import get_codex_session
        fresh = get_codex_session()
        self.assertEqual(fresh.process_state(), "stopped")


class CodexBridgeHandlerTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        reset_codex_session()
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = _make_fake_codex_bin(Path(self.tmp.name))
        self._env = mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(self.bin)})
        self._env.start()
        from codex import session as sess
        sess._SESSION = CodexSession(executable=str(self.bin))
        ensure_codex_registered()

    def tearDown(self):
        shutdown_provider_background()
        self._env.stop()
        reset_codex_session()
        reset_default_registry()
        self.tmp.cleanup()

    def test_bridge_connect_and_status(self):
        class _H(bridge.Handler):
            def __init__(self):
                self.client_address = ("127.0.0.1", 0)
                self._sent = {}

            def _send_json(self, status, obj):
                self._sent["status"] = status
                self._sent["body"] = obj

        h = _H()
        h._handle_providers_post("/api/providers/codex_chatgpt/connect", {"method": "browser"})
        self.assertEqual(h._sent["status"], 200)
        assert_safe_provider_payload(h._sent["body"])
        self.assertNotIn(SECRET, json.dumps(h._sent["body"]))
        h._handle_providers_get("/api/providers/codex_chatgpt/login/status")
        self.assertEqual(h._sent["status"], 200)
        assert_safe_provider_payload(h._sent["body"])


if __name__ == "__main__":
    unittest.main()

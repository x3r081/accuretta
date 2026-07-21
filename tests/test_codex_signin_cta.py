"""Regression: Codex sign-in CTA must not follow turn errors."""

from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex.inference import CodexInferenceService
from codex.inference_types import CodexInferenceAvailability
from codex.process import CodexProcessError
from codex.protocol import SafeAccountView
from providers.codex_readiness import (
    UI_REASON_NOT_SIGNED_IN,
    CodexInferenceStatus,
    assess_codex_inference_readiness,
)

ROOT = Path(__file__).resolve().parent.parent


def _fake_discovery():
    return mock.Mock(available=True, executable="/x", version="1", disabled_reason=None)


class CodexAuthPreservedOnProcessErrorTest(unittest.TestCase):
    def test_check_available_keeps_cached_auth_when_process_fails(self):
        svc = CodexInferenceService.__new__(CodexInferenceService)
        session = mock.Mock()
        discovery = mock.Mock(available=True, executable="/bin/codex", disabled_reason=None)
        session.discovery.return_value = discovery
        session.ensure_ready.side_effect = CodexProcessError("app-server exited")
        session.process_state.return_value = "error"
        acct = mock.Mock()
        acct.account = SafeAccountView(
            authenticated=True,
            auth_mode="chatgpt",
            account_label="user@example.com",
        )
        session._account = acct
        svc._session = session

        with mock.patch("codex.inference.is_codex_inference_enabled", return_value=True):
            avail = svc.check_available(live=True)

        self.assertTrue(avail.authenticated)
        self.assertFalse(avail.process_ready)
        self.assertFalse(avail.available)

    def test_readiness_process_down_while_signed_in_is_not_sign_in(self):
        avail = CodexInferenceAvailability(
            flag_enabled=True,
            cli_available=True,
            process_ready=False,
            authenticated=True,
            available=False,
            reason="Codex app-server is not ready",
        )
        svc = mock.Mock()
        svc.check_available.return_value = avail
        with mock.patch(
            "providers.codex_readiness.is_codex_inference_enabled", return_value=True
        ), mock.patch(
            "providers.codex_readiness.discover_codex", return_value=_fake_discovery()
        ), mock.patch(
            "providers.codex_readiness.get_codex_inference_service", return_value=svc
        ), mock.patch(
            "providers.codex_readiness._protocol_supported", return_value=True
        ):
            r = assess_codex_inference_readiness(live=True)
        self.assertEqual(r["status"], CodexInferenceStatus.PROCESS_UNAVAILABLE.value)
        self.assertTrue(r["authenticated"])
        self.assertNotEqual(r["selectionDisabledReason"], UI_REASON_NOT_SIGNED_IN)
        self.assertNotIn("Sign in", r["selectionDisabledReason"] or "")

    def test_readiness_unknown_auth_when_process_down_without_cache(self):
        avail = CodexInferenceAvailability(
            flag_enabled=True,
            cli_available=True,
            process_ready=False,
            authenticated=False,
            available=False,
            reason="Codex app-server exited",
        )
        svc = mock.Mock()
        svc.check_available.return_value = avail
        with mock.patch(
            "providers.codex_readiness.is_codex_inference_enabled", return_value=True
        ), mock.patch(
            "providers.codex_readiness.discover_codex", return_value=_fake_discovery()
        ), mock.patch(
            "providers.codex_readiness.get_codex_inference_service", return_value=svc
        ), mock.patch(
            "providers.codex_readiness._protocol_supported", return_value=True
        ):
            r = assess_codex_inference_readiness(live=True)
        self.assertNotEqual(r["status"], CodexInferenceStatus.NOT_SIGNED_IN.value)
        self.assertNotEqual(r["selectionDisabledReason"], UI_REASON_NOT_SIGNED_IN)

    def test_status_dto_preserves_account_after_ensure_ready_failure(self):
        from codex.session import CodexSession

        sess = CodexSession.__new__(CodexSession)
        sess._lock = threading.RLock()
        sess._discovery = mock.Mock(
            available=True,
            executable="/x",
            version="1",
            source="test",
            disabled_reason=None,
        )
        sess._process = None
        sess._last_error = None
        sess._explicit_executable = None
        sess._process_factory = None
        sess._login_pending_blocks_restart = False
        sess._extra_listeners = []
        sess._server_request_handler = None
        sess._server_req_box = {"handler": None}
        acct = mock.Mock()
        acct.account = SafeAccountView(authenticated=True, account_label="a@b.c")
        acct.pending = None
        sess._account = acct
        sess.ensure_ready = mock.Mock(side_effect=CodexProcessError("down"))
        sess.process_state = mock.Mock(return_value="error")
        with mock.patch("codex.session.is_codex_inference_enabled", return_value=True):
            dto = sess.status_dto(live=True)
        self.assertTrue(dto["authenticated"])
        self.assertEqual(dto["accountLabel"], "a@b.c")


class CodexSignInCtaUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")

    def test_auth_state_helper_exists(self):
        self.assertIn("function _codexAuthState", self.js)
        self.assertIn("function _codexSignInCtaReason", self.js)
        self.assertIn('"signed_out"', self.js)
        self.assertIn("Checking ChatGPT sign-in…", self.js)

    def test_sign_in_cta_only_when_signed_out(self):
        self.assertIn('if (_codexAuthState() !== "signed_out") return null;', self.js)
        self.assertIn('return "Sign in with ChatGPT first";', self.js)

    def test_send_gates_on_auth_not_selectable(self):
        send = self.js.split("async function send(", 1)[1].split("function setStreamingUI", 1)[0]
        self.assertIn("_codexAuthState()", send)
        self.assertNotIn("!cx.selectable", send)
        self.assertIn('auth === "signed_out"', send)

    def test_stream_end_refreshes_providers_live(self):
        self.assertIn("await loadProviders({ live: true })", self.js)

    def test_unavailable_bar_hides_when_no_reason(self):
        self.assertIn("bar.hidden = true", self.js)
        self.assertIn('id="provider-session-unavailable"', self.html)

    def test_local_session_never_sign_in_cta(self):
        fn = self.js.split("function _codexSignInCtaReason", 1)[1].split(
            "function _codexSessionUnavailableReason", 1
        )[0]
        self.assertIn('!== "codex_chatgpt"', fn)

    def test_signed_in_strips_stale_sign_in_reason(self):
        fn = self.js.split("function _codexSessionUnavailableReason", 1)[1].split(
            "function assistantResponseLabel", 1
        )[0]
        self.assertIn("/sign in with chatgpt/i.test", fn)


if __name__ == "__main__":
    unittest.main()

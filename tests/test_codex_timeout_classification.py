"""Regression: Codex turn vs approval timeout classification."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex.inference import CodexInferenceService
from codex.inference_types import CodexInferenceError, CodexInferenceEventType
from codex.timeouts import (
    DEFAULT_CODEX_TURN_TIMEOUT_S,
    MAX_CODEX_TURN_TIMEOUT_S,
    MIN_CODEX_TURN_TIMEOUT_S,
    get_codex_turn_timeout_seconds,
    normalize_codex_turn_timeout_seconds,
)
from providers.codex_errors import (
    MSG_APPROVAL_TIMEOUT,
    MSG_TIMEOUT,
    MSG_TURN_CANCELLED,
    user_message_for_codex_error,
)
from providers.codex_provider import CodexProvider


class CodexTimeoutNormalizeTest(unittest.TestCase):
    def test_default_and_presets(self):
        self.assertEqual(DEFAULT_CODEX_TURN_TIMEOUT_S, 900)
        self.assertEqual(normalize_codex_turn_timeout_seconds(None), 900)
        self.assertEqual(normalize_codex_turn_timeout_seconds(""), 900)
        self.assertEqual(normalize_codex_turn_timeout_seconds("900"), 900)
        self.assertEqual(normalize_codex_turn_timeout_seconds(300), 300)

    def test_clamp_out_of_range(self):
        self.assertEqual(normalize_codex_turn_timeout_seconds(30), MIN_CODEX_TURN_TIMEOUT_S)
        self.assertEqual(normalize_codex_turn_timeout_seconds(99999), MAX_CODEX_TURN_TIMEOUT_S)

    def test_malformed_falls_back(self):
        self.assertEqual(normalize_codex_turn_timeout_seconds("nope"), 900)
        self.assertEqual(normalize_codex_turn_timeout_seconds({}), 900)
        self.assertEqual(normalize_codex_turn_timeout_seconds(True), 900)

    def test_get_from_settings(self):
        self.assertEqual(get_codex_turn_timeout_seconds({"codex_turn_timeout_seconds": 600}), 600)
        self.assertEqual(get_codex_turn_timeout_seconds({"codex_turn_timeout_seconds": "bad"}), 900)


class CodexTimeoutErrorMappingTest(unittest.TestCase):
    def test_turn_timeout_type_maps_to_msg_timeout(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex did not finish within the configured turn timeout.",
                event_type=CodexInferenceEventType.TURN_TIMEOUT,
            )
        )
        self.assertEqual(msg, MSG_TIMEOUT)
        self.assertNotIn("approval", msg.lower())

    def test_approval_timeout_type_maps_to_approval(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "The approval request timed out before a decision was made.",
                event_type=CodexInferenceEventType.APPROVAL_TIMEOUT,
            )
        )
        self.assertEqual(msg, MSG_APPROVAL_TIMEOUT)
        self.assertIn("approval", msg.lower())

    def test_cancelled_type(self):
        msg = user_message_for_codex_error(
            CodexInferenceError("x", event_type=CodexInferenceEventType.CANCELLED)
        )
        self.assertEqual(msg, MSG_TURN_CANCELLED)

    def test_legacy_ambiguous_string_is_general_timeout(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex turn timed out while waiting for approval or a reply",
                event_type=CodexInferenceEventType.PROTOCOL_ERROR,
            )
        )
        self.assertEqual(msg, MSG_TIMEOUT)
        self.assertNotEqual(msg, MSG_APPROVAL_TIMEOUT)
        self.assertNotIn("Approve or deny", msg)

    def test_legacy_string_without_type_still_general(self):
        class _E(Exception):
            def __init__(self):
                self.message = (
                    "Codex turn timed out while waiting for approval or a reply"
                )
                self.code = "protocol_error"

        msg = user_message_for_codex_error(_E())
        self.assertEqual(msg, MSG_TIMEOUT)


class CodexTurnTimeoutRaiseTest(unittest.TestCase):
    def test_overall_timeout_empty_pending_is_turn_timeout(self):
        svc = CodexInferenceService.__new__(CodexInferenceService)
        svc._session = mock.Mock()
        svc._session.process_state.return_value = "ready"
        rpc = mock.Mock()
        rpc.request.return_value = {
            "turn": {"id": "tu-1", "status": "inProgress"},
            "turnId": "tu-1",
        }
        svc._session.rpc = rpc
        svc._session.ensure_ready.return_value = mock.Mock(
            account=mock.Mock(authenticated=True)
        )
        svc._ensure_notification_hook = mock.Mock()
        svc._busy = threading.Lock()
        svc._state_lock = threading.RLock()
        svc._terminal_event = threading.Event()
        svc._terminal = None
        svc._text_parts = []
        svc._event_sink = None
        svc._pending_approvals = {}
        svc._active_thread_id = None
        svc._active_turn_id = None
        svc._active_generation = 0
        svc._cancel_requested = False
        svc._file_change_items = {}
        svc._registered = True
        svc.cancel_active_turn = mock.Mock(return_value=True)
        svc._decline_pending_approvals = mock.Mock()

        with mock.patch.object(svc, "_assert_can_infer"):
            with self.assertRaises(CodexInferenceError) as ctx:
                svc.run_turn("th-1", "long task", timeout_s=0.3)

        self.assertEqual(ctx.exception.event_type, CodexInferenceEventType.TURN_TIMEOUT)
        self.assertNotIn("approval", ctx.exception.message.lower())
        mapped = user_message_for_codex_error(ctx.exception)
        self.assertEqual(mapped, MSG_TIMEOUT)
        self.assertNotIn("approval", mapped.lower())
        svc._decline_pending_approvals.assert_called()

    def test_configured_timeout_passed_to_run_turn(self):
        seen = {}

        class _Svc:
            def run_turn(self, thread_id, text, *, on_event=None, timeout_s=900.0):
                seen["timeout_s"] = timeout_s
                raise CodexInferenceError(
                    "Codex did not finish within the configured turn timeout.",
                    event_type=CodexInferenceEventType.TURN_TIMEOUT,
                )

        with mock.patch(
            "providers.codex_provider.get_codex_turn_timeout_seconds",
            return_value=1200,
        ), mock.patch(
            "providers.codex_provider.get_codex_inference_service",
            return_value=_Svc(),
        ):
            # Exercise the worker path's timeout lookup indirectly.
            timeout_s = float(
                __import__(
                    "providers.codex_provider", fromlist=["get_codex_turn_timeout_seconds"]
                ).get_codex_turn_timeout_seconds()
            )
            svc = _Svc()
            try:
                svc.run_turn("t", "x", timeout_s=timeout_s)
            except CodexInferenceError:
                pass
        self.assertEqual(seen["timeout_s"], 1200.0)
        self.assertGreater(seen["timeout_s"], 300.0)

    def test_approval_ui_timeout_signals_approval_timeout(self):
        svc = CodexInferenceService.__new__(CodexInferenceService)
        svc._session = mock.Mock()
        rpc = mock.Mock()
        svc._session.rpc = rpc
        svc._state_lock = threading.RLock()
        svc._pending_approvals = {}
        svc._file_change_items = {}
        svc._active_workspace_cwd = None
        svc._active_thread_id = "th-1"
        svc._active_turn_id = "tu-1"
        svc._terminal = None
        svc._terminal_event = threading.Event()
        svc._event_sink = None
        svc._emit = mock.Mock()

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p"
            ws.mkdir()
            target = str(ws / "f.txt")
            svc._active_workspace_cwd = str(ws)
            svc._file_change_items = {
                "item-1": {"changes": [{"path": target, "kind": "add"}]}
            }
            with mock.patch(
                "codex.inference.get_codex_write_mode",
                return_value="ask",
            ), mock.patch(
                "bridge.get_settings",
                return_value={"auto_approve_write": False},
            ), mock.patch(
                "bridge.request_approval",
                return_value={"status": "timeout", "decision": "deny"},
            ):
                svc._handle_server_request({
                    "id": 7,
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "threadId": "th-1",
                        "turnId": "tu-1",
                        "itemId": "item-1",
                    },
                })

        self.assertTrue(svc._terminal_event.is_set())
        self.assertIsNotNone(svc._terminal)
        self.assertEqual(
            svc._terminal.type, CodexInferenceEventType.APPROVAL_TIMEOUT
        )
        mapped = user_message_for_codex_error(
            CodexInferenceError(
                svc._terminal.message or "",
                event_type=svc._terminal.type,
            )
        )
        self.assertEqual(mapped, MSG_APPROVAL_TIMEOUT)
        self.assertIn("approval", mapped.lower())
        self.assertEqual(svc._pending_approvals, {})

    def test_approved_request_does_not_signal_approval_timeout(self):
        svc = CodexInferenceService.__new__(CodexInferenceService)
        svc._session = mock.Mock()
        rpc = mock.Mock()
        svc._session.rpc = rpc
        svc._state_lock = threading.RLock()
        svc._pending_approvals = {}
        svc._file_change_items = {}
        svc._active_thread_id = "th-1"
        svc._active_turn_id = "tu-1"
        svc._terminal = None
        svc._terminal_event = threading.Event()
        svc._event_sink = None
        svc._emit = mock.Mock()

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p"
            ws.mkdir()
            target = str(ws / "f.txt")
            svc._active_workspace_cwd = str(ws)
            svc._file_change_items = {
                "item-1": {"changes": [{"path": target}]}
            }
            with mock.patch(
                "codex.inference.get_codex_write_mode",
                return_value="ask",
            ), mock.patch(
                "bridge.get_settings",
                return_value={},
            ), mock.patch(
                "bridge.request_approval",
                return_value={"status": "decided", "decision": "approve"},
            ):
                svc._handle_server_request({
                    "id": 8,
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "threadId": "th-1",
                        "turnId": "tu-1",
                        "itemId": "item-1",
                    },
                })

        self.assertFalse(svc._terminal_event.is_set())
        self.assertIsNone(svc._terminal)
        rpc.respond.assert_called_once_with(8, {"decision": "accept"})
        self.assertEqual(svc._pending_approvals, {})


class CodexTimeoutUiGuardsTest(unittest.TestCase):
    def test_js_does_not_show_sign_in_when_authenticated(self):
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function _codexAuthState", js)
        self.assertIn("function _codexSignInCtaReason", js)
        self.assertIn('if (_codexAuthState() !== "signed_out") return null;', js)
        self.assertIn("Checking ChatGPT sign-in…", js)
        self.assertIn("set-codex-turn-timeout", js)

    def test_default_settings_include_timeout(self):
        import bridge
        self.assertEqual(bridge.DEFAULT_SETTINGS.get("codex_turn_timeout_seconds"), 900)

    def test_html_exposes_timeout_control(self):
        html = (Path(__file__).resolve().parent.parent / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Codex task timeout", html)
        self.assertIn('id="set-codex-turn-timeout"', html)
        self.assertIn("Approval dialogs have their own separate timeout", html)


class CodexTimeoutHandshakeMessageTest(unittest.TestCase):
    """Update prior handshake expectations: ambiguous string → MSG_TIMEOUT."""

    def test_old_ambiguous_no_longer_approval(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex turn timed out while waiting for approval or a reply",
                event_type=CodexInferenceEventType.PROTOCOL_ERROR,
            )
        )
        self.assertEqual(msg, MSG_TIMEOUT)


if __name__ == "__main__":
    unittest.main()

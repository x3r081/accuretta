"""Regression: Codex 0.144.6 approval handshake, timeouts, streaming cleanup."""

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

from codex.approvals import (
    APPROVAL_UI_TIMEOUT_S,
    WRITE_MODE_ASK,
    WRITE_MODE_CHAT_ONLY,
    WRITE_MODE_WORKSPACE_AUTO,
    approval_policy_for_write_mode,
    decide_file_change,
    sandbox_for_write_mode,
)
from codex.inference import CodexInferenceService
from codex.inference_types import CodexInferenceError, CodexInferenceEventType
from providers.codex_errors import (
    MSG_APPROVAL_TIMEOUT,
    MSG_APP_SERVER_EXITED,
    MSG_PROTOCOL_FAILURE,
    MSG_TIMEOUT,
    user_message_for_codex_error,
)


class Codex01446ProtocolContractTest(unittest.TestCase):
    def test_ask_mode_forces_untrusted_policy(self):
        # Codex 0.144.6: workspace-write + on-request does NOT emit approvals.
        self.assertEqual(approval_policy_for_write_mode(WRITE_MODE_ASK), "untrusted")
        self.assertEqual(sandbox_for_write_mode(WRITE_MODE_ASK), "workspace-write")
        self.assertEqual(
            approval_policy_for_write_mode(WRITE_MODE_WORKSPACE_AUTO), "on-request"
        )

    def test_accept_and_decline_payload_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "proj"
            ws.mkdir()
            target = str(ws / "a.txt")
            accept = decide_file_change(
                mode=WRITE_MODE_ASK,
                params={"itemId": "x", "threadId": "t", "turnId": "u"},
                workspace_cwd=str(ws),
                item_cache={"x": {"changes": [{"path": target, "kind": "add"}]}},
                request_approval=lambda *a, **k: {"decision": "approve"},
            )
            self.assertEqual(accept, {"decision": "accept"})
            deny = decide_file_change(
                mode=WRITE_MODE_ASK,
                params={"itemId": "x"},
                workspace_cwd=str(ws),
                item_cache={"x": {"changes": [{"path": target}]}},
                request_approval=lambda *a, **k: {"decision": "deny"},
            )
            self.assertEqual(deny, {"decision": "decline"})

    def test_paths_come_from_item_cache_not_approval_params(self):
        """0.144.6 approval params omit paths; they live on item/started."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "proj"
            ws.mkdir()
            outside = Path(tmp) / "other"
            outside.mkdir()
            bad = str(outside / "x.txt")
            # Params look like real 0.144.6 (no paths).
            params = {
                "grantRoot": None,
                "itemId": "exec-1",
                "reason": "",
                "startedAtMs": 1,
                "threadId": "th",
                "turnId": "tu",
            }
            out = decide_file_change(
                mode=WRITE_MODE_ASK,
                params=params,
                workspace_cwd=str(ws),
                item_cache={"exec-1": {"changes": [{"path": bad}]}},
                request_approval=lambda *a, **k: {"decision": "approve"},
            )
            self.assertEqual(out["decision"], "decline")

    def test_trust_writes_auto_approves_in_workspace_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "proj"
            ws.mkdir()
            target = str(ws / "ok.txt")
            outside = str(Path(tmp) / "nope.txt")
            Path(tmp, "nope.txt").write_text("x")
            ok = decide_file_change(
                mode=WRITE_MODE_ASK,
                params={"itemId": "1"},
                workspace_cwd=str(ws),
                item_cache={"1": {"changes": [{"path": target}]}},
                request_approval=lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompt")),
                trust_writes=True,
            )
            self.assertEqual(ok["decision"], "accept")
            blocked = decide_file_change(
                mode=WRITE_MODE_ASK,
                params={"itemId": "2"},
                workspace_cwd=str(ws),
                item_cache={"2": {"changes": [{"path": outside}]}},
                request_approval=lambda *a, **k: {"decision": "approve"},
                trust_writes=True,
            )
            self.assertEqual(blocked["decision"], "decline")

    def test_approval_ui_timeout_is_bounded(self):
        self.assertLessEqual(APPROVAL_UI_TIMEOUT_S, 120)
        self.assertGreaterEqual(APPROVAL_UI_TIMEOUT_S, 30)


class CodexApprovalHandlerTest(unittest.TestCase):
    def _svc_with_rpc(self):
        svc = CodexInferenceService.__new__(CodexInferenceService)
        svc._session = mock.Mock()
        rpc = mock.Mock()
        svc._session.rpc = rpc
        svc._state_lock = threading.RLock()
        svc._active_workspace_cwd = None
        svc._file_change_items = {}
        svc._pending_approvals = {}
        svc._active_thread_id = "th-1"
        svc._active_turn_id = "tu-1"
        svc._event_sink = None
        svc._emit = mock.Mock()
        return svc, rpc

    def test_preserves_jsonrpc_id_zero(self):
        svc, rpc = self._svc_with_rpc()
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p"
            ws.mkdir()
            target = str(ws / "f.txt")
            svc._active_workspace_cwd = str(ws)
            svc._file_change_items = {
                "item-1": {"changes": [{"path": target, "kind": "add"}]}
            }
            with mock.patch(
                "codex.inference.get_codex_write_mode", return_value=WRITE_MODE_ASK
            ), mock.patch(
                "bridge.get_settings", return_value={"auto_approve_write": False}
            ), mock.patch(
                "bridge.request_approval",
                return_value={"decision": "approve", "status": "decided"},
            ):
                svc._handle_server_request({
                    "id": 0,
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "threadId": "th-1",
                        "turnId": "tu-1",
                        "itemId": "item-1",
                        "grantRoot": None,
                        "reason": "",
                    },
                })
        rpc.respond.assert_called_once_with(0, {"decision": "accept"})

    def test_deny_response_shape(self):
        svc, rpc = self._svc_with_rpc()
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p"
            ws.mkdir()
            target = str(ws / "f.txt")
            svc._active_workspace_cwd = str(ws)
            svc._file_change_items = {"item-1": {"changes": [{"path": target}]}}
            with mock.patch(
                "codex.inference.get_codex_write_mode", return_value=WRITE_MODE_ASK
            ), mock.patch(
                "bridge.get_settings", return_value={"auto_approve_write": False}
            ), mock.patch(
                "bridge.request_approval",
                return_value={"decision": "deny", "status": "decided"},
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
        rpc.respond.assert_called_once_with(7, {"decision": "decline"})

    def test_duplicate_request_id_not_double_prompted(self):
        svc, rpc = self._svc_with_rpc()
        calls = []

        def slow_approve(*a, **k):
            calls.append(1)
            time.sleep(0.05)
            return {"decision": "approve"}

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p"
            ws.mkdir()
            svc._active_workspace_cwd = str(ws)
            svc._file_change_items = {
                "i": {"changes": [{"path": str(ws / "a.txt")}]}
            }
            svc._pending_approvals[0] = {"method": "item/fileChange/requestApproval"}
            with mock.patch(
                "codex.inference.get_codex_write_mode", return_value=WRITE_MODE_ASK
            ), mock.patch(
                "bridge.get_settings", return_value={}
            ), mock.patch(
                "bridge.request_approval", side_effect=slow_approve
            ):
                svc._handle_server_request({
                    "id": 0,
                    "method": "item/fileChange/requestApproval",
                    "params": {"threadId": "th-1", "turnId": "tu-1", "itemId": "i"},
                })
        self.assertEqual(calls, [])
        rpc.respond.assert_not_called()

    def test_stale_turn_decision_declined(self):
        svc, rpc = self._svc_with_rpc()
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p"
            ws.mkdir()
            svc._active_workspace_cwd = str(ws)
            svc._active_turn_id = "tu-NEW"
            svc._file_change_items = {
                "i": {"changes": [{"path": str(ws / "a.txt")}]}
            }
            with mock.patch(
                "codex.inference.get_codex_write_mode", return_value=WRITE_MODE_ASK
            ), mock.patch(
                "bridge.get_settings", return_value={}
            ), mock.patch(
                "bridge.request_approval",
                return_value={"decision": "approve"},
            ):
                svc._handle_server_request({
                    "id": 3,
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "threadId": "th-1",
                        "turnId": "tu-OLD",
                        "itemId": "i",
                    },
                })
        rpc.respond.assert_called_once_with(3, {"decision": "decline"})

    def test_cancel_declines_pending(self):
        svc, rpc = self._svc_with_rpc()
        svc._pending_approvals[9] = {
            "method": "item/fileChange/requestApproval",
            "threadId": "th-1",
            "turnId": "tu-1",
        }
        svc._decline_pending_approvals(reason="cancel")
        rpc.respond.assert_called_once_with(9, {"decision": "decline"})
        self.assertEqual(svc._pending_approvals, {})

    def test_chat_only_declines_without_ui(self):
        svc, rpc = self._svc_with_rpc()
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p"
            ws.mkdir()
            svc._active_workspace_cwd = str(ws)
            with mock.patch(
                "codex.inference.get_codex_write_mode",
                return_value=WRITE_MODE_CHAT_ONLY,
            ), mock.patch(
                "bridge.get_settings", return_value={}
            ), mock.patch(
                "bridge.request_approval",
                side_effect=AssertionError("no ui"),
            ):
                svc._handle_server_request({
                    "id": 1,
                    "method": "item/fileChange/requestApproval",
                    "params": {"threadId": "th-1", "turnId": "tu-1", "itemId": "i"},
                })
        rpc.respond.assert_called_once_with(1, {"decision": "decline"})


class CodexErrorMessageTest(unittest.TestCase):
    def test_timeout_does_not_blame_cli_version(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex did not finish within the configured turn timeout.",
                event_type=CodexInferenceEventType.TURN_TIMEOUT,
            )
        )
        self.assertEqual(msg, MSG_TIMEOUT)
        self.assertNotIn("CLI version", msg)
        self.assertNotIn("approval", msg.lower())

    def test_legacy_ambiguous_maps_to_general_timeout(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex turn timed out while waiting for approval or a reply",
                event_type=CodexInferenceEventType.PROTOCOL_ERROR,
            )
        )
        self.assertEqual(msg, MSG_TIMEOUT)
        self.assertNotEqual(msg, MSG_APPROVAL_TIMEOUT)

    def test_generic_timeout_message(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex turn timed out",
                event_type=CodexInferenceEventType.PROTOCOL_ERROR,
            )
        )
        self.assertEqual(msg, MSG_TIMEOUT)
        self.assertNotEqual(msg, MSG_PROTOCOL_FAILURE)

    def test_approval_timeout_type(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "The approval request timed out before a decision was made.",
                event_type=CodexInferenceEventType.APPROVAL_TIMEOUT,
            )
        )
        self.assertEqual(msg, MSG_APPROVAL_TIMEOUT)

    def test_process_exit_message(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex app-server process failed during turn",
                event_type=CodexInferenceEventType.PROCESS_ERROR,
            )
        )
        self.assertEqual(msg, MSG_APP_SERVER_EXITED)

    def test_streaming_error_handler_clears_footer(self):
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('evt.type === "error"', js)
        self.assertIn('meta.classList.remove("streaming")', js)
        self.assertIn("waiting_for_approval", js)


class CodexSseStatusPassthroughTest(unittest.TestCase):
    def test_status_events_become_notices(self):
        from providers.base import InferenceEvent, InferenceEventType
        from providers.inference_stream import _inference_events_to_openai_sse
        import json

        events = [
            InferenceEvent(
                event_type=InferenceEventType.STATUS,
                error="Codex is waiting for file-write approval.",
                raw={"status": "waiting_for_approval", "note": "Codex is waiting for file-write approval."},
            )
        ]
        chunks = list(_inference_events_to_openai_sse(iter(events)))
        text = b"".join(chunks).decode("utf-8")
        self.assertIn("accuretta_notice", text)
        self.assertIn("waiting for file-write approval", text)
        # parse the first data line
        line = [l for l in text.splitlines() if l.startswith("data: ")][0]
        obj = json.loads(line[6:])
        self.assertEqual(obj["accuretta_status"], "waiting_for_approval")


if __name__ == "__main__":
    unittest.main()

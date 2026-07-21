"""Regression: Codex idle watchdog, hard-max, and approval timeout."""

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
from codex.inference_types import (
    CodexInferenceError,
    CodexInferenceEvent,
    CodexInferenceEventType,
)
from codex.timeouts import (
    ACTIVITY_OUTPUT,
    ACTIVITY_TOOL_PROGRESS,
    ACTIVITY_TOOL_START,
    DEFAULT_CODEX_IDLE_TIMEOUT_S,
    MAX_CODEX_IDLE_TIMEOUT_S,
    MIN_CODEX_IDLE_TIMEOUT_S,
    get_codex_idle_timeout_seconds,
    get_codex_max_task_duration_seconds,
    migrate_codex_timeout_settings,
    normalize_codex_idle_timeout_seconds,
    normalize_codex_max_task_duration_seconds,
)
from providers.codex_errors import (
    MSG_APPROVAL_TIMEOUT,
    MSG_IDLE_TIMEOUT,
    MSG_MAX_TASK_DURATION,
    MSG_TIMEOUT,
    MSG_TURN_CANCELLED,
    user_message_for_codex_error,
)


def _bare_service() -> CodexInferenceService:
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
    svc._active_workspace_cwd = None
    svc._turn_started_monotonic = 0.0
    svc._last_activity_monotonic = 0.0
    svc._last_activity_type = None
    svc._last_activity_event = None
    svc._idle_timeout_s = float(DEFAULT_CODEX_IDLE_TIMEOUT_S)
    svc._max_duration_s = None
    svc._watchdog_generation = 0
    svc.cancel_active_turn = mock.Mock(return_value=True)
    svc._decline_pending_approvals = mock.Mock()
    return svc


class CodexIdleTimeoutNormalizeTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULT_CODEX_IDLE_TIMEOUT_S, 300)
        self.assertEqual(normalize_codex_idle_timeout_seconds(None), 300)
        self.assertEqual(normalize_codex_idle_timeout_seconds(""), 300)
        self.assertEqual(normalize_codex_idle_timeout_seconds("300"), 300)

    def test_clamp(self):
        self.assertEqual(normalize_codex_idle_timeout_seconds(30), MIN_CODEX_IDLE_TIMEOUT_S)
        self.assertEqual(normalize_codex_idle_timeout_seconds(99999), MAX_CODEX_IDLE_TIMEOUT_S)

    def test_malformed(self):
        self.assertEqual(normalize_codex_idle_timeout_seconds("nope"), 300)
        self.assertEqual(normalize_codex_idle_timeout_seconds({}), 300)
        self.assertEqual(normalize_codex_idle_timeout_seconds(True), 300)

    def test_max_duration_disabled_by_default(self):
        self.assertIsNone(normalize_codex_max_task_duration_seconds(None))
        self.assertIsNone(normalize_codex_max_task_duration_seconds(0))
        self.assertIsNone(normalize_codex_max_task_duration_seconds("disabled"))
        self.assertIsNone(normalize_codex_max_task_duration_seconds("bad"))
        self.assertEqual(normalize_codex_max_task_duration_seconds(900), 900)
        self.assertEqual(normalize_codex_max_task_duration_seconds(100), 900)
        self.assertEqual(normalize_codex_max_task_duration_seconds(99999), 14400)

    def test_get_idle_ignores_deprecated_wall_clock_key(self):
        # Old 900s wall-clock must NOT become a 900s idle timeout.
        self.assertEqual(
            get_codex_idle_timeout_seconds({"codex_turn_timeout_seconds": 900}),
            300,
        )
        self.assertEqual(
            get_codex_idle_timeout_seconds({"codex_idle_timeout_seconds": 600}),
            600,
        )

    def test_get_max_duration(self):
        self.assertIsNone(get_codex_max_task_duration_seconds({}))
        self.assertEqual(
            get_codex_max_task_duration_seconds({"codex_max_task_duration_seconds": 1800}),
            1800,
        )

    def test_migration_strips_deprecated_and_sets_idle_default(self):
        out, changed = migrate_codex_timeout_settings({
            "codex_turn_timeout_seconds": 900,
            "theme": "light",
        })
        self.assertTrue(changed)
        self.assertNotIn("codex_turn_timeout_seconds", out)
        self.assertEqual(out["codex_idle_timeout_seconds"], 300)
        self.assertEqual(out["codex_max_task_duration_seconds"], 0)


class CodexTimeoutErrorMappingTest(unittest.TestCase):
    def test_idle_timeout_message(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex stopped producing activity during the configured idle period.",
                event_type=CodexInferenceEventType.IDLE_TIMEOUT,
            )
        )
        self.assertEqual(msg, MSG_IDLE_TIMEOUT)
        self.assertIn("stuck", msg.lower())
        self.assertNotIn("approval", msg.lower())

    def test_deprecated_turn_timeout_alias_maps_to_idle(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "legacy",
                event_type=CodexInferenceEventType.TURN_TIMEOUT,
            )
        )
        self.assertEqual(msg, MSG_IDLE_TIMEOUT)
        self.assertEqual(MSG_TIMEOUT, MSG_IDLE_TIMEOUT)

    def test_max_task_duration_message(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex reached the configured maximum task duration.",
                event_type=CodexInferenceEventType.MAX_TASK_DURATION,
            )
        )
        self.assertEqual(msg, MSG_MAX_TASK_DURATION)
        self.assertIn("safety limit", msg.lower())
        self.assertNotIn("stuck", msg.lower())
        self.assertNotIn("approval", msg.lower())

    def test_approval_timeout_type(self):
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

    def test_legacy_ambiguous_string_is_idle(self):
        msg = user_message_for_codex_error(
            CodexInferenceError(
                "Codex turn timed out while waiting for approval or a reply",
                event_type=CodexInferenceEventType.PROTOCOL_ERROR,
            )
        )
        self.assertEqual(msg, MSG_IDLE_TIMEOUT)
        self.assertNotEqual(msg, MSG_APPROVAL_TIMEOUT)


class CodexIdleWatchdogTest(unittest.TestCase):
    def test_a_activity_prevents_idle_timeout(self):
        svc = _bare_service()

        def _keep_alive_then_complete():
            for _ in range(80):
                if svc._active_turn_id:
                    break
                time.sleep(0.02)
            tid = svc._active_turn_id
            for _ in range(6):
                svc.record_turn_activity("th-1", tid, ACTIVITY_OUTPUT, event_name="delta")
                time.sleep(0.12)
            with svc._state_lock:
                svc._terminal = CodexInferenceEvent(
                    type=CodexInferenceEventType.COMPLETED,
                    thread_id="th-1",
                    turn_id=tid,
                    status="completed",
                )
                svc._terminal_event.set()

        threading.Thread(target=_keep_alive_then_complete, daemon=True).start()
        with mock.patch.object(svc, "_assert_can_infer"):
            result = svc.run_turn(
                "th-1", "long active", idle_timeout_s=0.35, max_duration_s=None
            )
        self.assertTrue(result.ok)
        self.assertEqual(svc._watchdog_generation, 0)

    def test_b_true_inactivity_raises_idle_timeout(self):
        svc = _bare_service()
        with mock.patch.object(svc, "_assert_can_infer"):
            with self.assertRaises(CodexInferenceError) as ctx:
                svc.run_turn("th-1", "stall", idle_timeout_s=0.35)
        self.assertEqual(ctx.exception.event_type, CodexInferenceEventType.IDLE_TIMEOUT)
        self.assertNotIn("approval", ctx.exception.message.lower())
        mapped = user_message_for_codex_error(ctx.exception)
        self.assertEqual(mapped, MSG_IDLE_TIMEOUT)
        self.assertNotIn("approval", mapped.lower())
        self.assertEqual(svc._watchdog_generation, 0)

    def test_c_irrelevant_events_do_not_reset(self):
        svc = _bare_service()
        svc._active_generation = 1
        svc._watchdog_generation = 1
        svc._active_thread_id = "th-1"
        svc._active_turn_id = "tu-1"
        svc._last_activity_monotonic = time.monotonic() - 10
        before = svc._last_activity_monotonic
        # Account / unrelated methods are ignored by _on_notification.
        svc._on_notification("account/updated", {"threadId": "th-1"})
        svc._on_notification("account/rateLimits/updated", {})
        self.assertEqual(svc._last_activity_monotonic, before)
        # Wrong thread must not reset.
        ok = svc.record_turn_activity("other", "tu-1", ACTIVITY_OUTPUT, event_name="x")
        self.assertFalse(ok)
        self.assertEqual(svc._last_activity_monotonic, before)

    def test_d_old_turn_events_do_not_keep_alive(self):
        svc = _bare_service()
        svc._active_generation = 2
        svc._watchdog_generation = 2
        svc._active_thread_id = "th-1"
        svc._active_turn_id = "tu-new"
        svc._last_activity_monotonic = time.monotonic() - 5
        before = svc._last_activity_monotonic
        ok = svc.record_turn_activity(
            "th-1", "tu-old", ACTIVITY_OUTPUT, event_name="stale"
        )
        self.assertFalse(ok)
        self.assertEqual(svc._last_activity_monotonic, before)

    def test_e_tool_progress_resets_idle(self):
        svc = _bare_service()
        svc._active_generation = 1
        svc._watchdog_generation = 1
        svc._active_thread_id = "th-1"
        svc._active_turn_id = "tu-1"
        svc._last_activity_monotonic = time.monotonic() - 50
        svc._on_notification(
            "item/commandExecution/started",
            {"threadId": "th-1", "turnId": "tu-1", "item": {"type": "commandExecution"}},
        )
        self.assertEqual(svc._last_activity_type, ACTIVITY_TOOL_START)
        mid = svc._last_activity_monotonic
        time.sleep(0.02)
        svc._on_notification(
            "item/commandExecution/outputDelta",
            {"threadId": "th-1", "turnId": "tu-1"},
        )
        self.assertEqual(svc._last_activity_type, ACTIVITY_TOOL_PROGRESS)
        self.assertGreater(svc._last_activity_monotonic, mid)

    def test_f_output_streaming_resets_idle(self):
        svc = _bare_service()
        svc._active_generation = 1
        svc._watchdog_generation = 1
        svc._active_thread_id = "th-1"
        svc._active_turn_id = "tu-1"
        svc._last_activity_monotonic = time.monotonic() - 50
        svc._on_notification(
            "item/agentMessage/delta",
            {"threadId": "th-1", "turnId": "tu-1", "delta": "hello"},
        )
        self.assertEqual(svc._last_activity_type, ACTIVITY_OUTPUT)
        self.assertIn("hello", "".join(svc._text_parts))

    def test_g_approval_pending_suspends_idle(self):
        svc = _bare_service()

        def _hold_approval_then_timeout():
            for _ in range(80):
                if svc._active_turn_id:
                    break
                time.sleep(0.02)
            with svc._state_lock:
                svc._pending_approvals["req-1"] = {
                    "method": "item/fileChange/requestApproval",
                    "threadId": "th-1",
                    "turnId": svc._active_turn_id,
                    "started": time.time(),
                }
            # Stay past idle without activity — must not raise IDLE_TIMEOUT.
            time.sleep(0.55)
            with svc._state_lock:
                svc._terminal = CodexInferenceEvent(
                    type=CodexInferenceEventType.APPROVAL_TIMEOUT,
                    thread_id="th-1",
                    turn_id=svc._active_turn_id,
                    message="The approval request timed out before a decision was made.",
                )
                svc._terminal_event.set()

        threading.Thread(target=_hold_approval_then_timeout, daemon=True).start()
        with mock.patch.object(svc, "_assert_can_infer"):
            result = svc.run_turn("th-1", "needs approval", idle_timeout_s=0.3)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, CodexInferenceEventType.APPROVAL_TIMEOUT.value)
        mapped = user_message_for_codex_error(
            CodexInferenceError(
                result.error_message or "",
                event_type=CodexInferenceEventType.APPROVAL_TIMEOUT,
            )
        )
        self.assertEqual(mapped, MSG_APPROVAL_TIMEOUT)

    def test_h_approval_accepted_refreshes_activity(self):
        svc = _bare_service()
        svc._session.rpc = mock.Mock()
        svc._active_generation = 1
        svc._watchdog_generation = 1
        svc._active_thread_id = "th-1"
        svc._active_turn_id = "tu-1"
        svc._emit = mock.Mock()
        svc._last_activity_monotonic = time.monotonic() - 40
        before = svc._last_activity_monotonic

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "p"
            ws.mkdir()
            target = str(ws / "f.txt")
            svc._active_workspace_cwd = str(ws)
            svc._file_change_items = {"item-1": {"changes": [{"path": target}]}}
            with mock.patch(
                "codex.inference.get_codex_write_mode", return_value="ask"
            ), mock.patch(
                "bridge.get_settings", return_value={}
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
        self.assertGreater(svc._last_activity_monotonic, before)
        self.assertIn(
            svc._last_activity_type,
            {"approval_requested", "approval_resolved"},
        )
        self.assertFalse(svc._terminal_event.is_set())

    def test_i_optional_maximum_disabled_allows_long_active_task(self):
        svc = _bare_service()

        def _pulse_then_complete():
            for _ in range(80):
                if svc._active_turn_id:
                    break
                time.sleep(0.02)
            tid = svc._active_turn_id
            # Exceed old 300ms-style short limits while staying active.
            for _ in range(8):
                svc.record_turn_activity("th-1", tid, ACTIVITY_OUTPUT, event_name="delta")
                time.sleep(0.08)
            with svc._state_lock:
                svc._terminal = CodexInferenceEvent(
                    type=CodexInferenceEventType.COMPLETED,
                    thread_id="th-1",
                    turn_id=tid,
                )
                svc._terminal_event.set()

        threading.Thread(target=_pulse_then_complete, daemon=True).start()
        t0 = time.monotonic()
        with mock.patch.object(svc, "_assert_can_infer"):
            result = svc.run_turn(
                "th-1", "long", idle_timeout_s=0.5, max_duration_s=None
            )
        elapsed = time.monotonic() - t0
        self.assertTrue(result.ok)
        self.assertGreater(elapsed, 0.5)

    def test_j_optional_maximum_enabled(self):
        svc = _bare_service()

        def _always_active():
            for _ in range(80):
                if svc._active_turn_id:
                    break
                time.sleep(0.02)
            tid = svc._active_turn_id
            while svc._watchdog_generation:
                svc.record_turn_activity("th-1", tid, ACTIVITY_OUTPUT, event_name="delta")
                time.sleep(0.05)

        threading.Thread(target=_always_active, daemon=True).start()
        with mock.patch.object(svc, "_assert_can_infer"):
            with self.assertRaises(CodexInferenceError) as ctx:
                svc.run_turn(
                    "th-1", "capped", idle_timeout_s=30.0, max_duration_s=0.4
                )
        self.assertEqual(ctx.exception.event_type, CodexInferenceEventType.MAX_TASK_DURATION)
        mapped = user_message_for_codex_error(ctx.exception)
        self.assertEqual(mapped, MSG_MAX_TASK_DURATION)
        self.assertIn("safety limit", mapped.lower())
        self.assertNotIn("stuck", mapped.lower())
        self.assertNotIn("approval", mapped.lower())

    def test_k_process_exit(self):
        svc = _bare_service()
        svc._session.process_state.side_effect = ["ready", "error", "error", "error"]
        with mock.patch.object(svc, "_assert_can_infer"):
            with self.assertRaises(CodexInferenceError) as ctx:
                svc.run_turn("th-1", "x", idle_timeout_s=5.0)
        self.assertEqual(ctx.exception.event_type, CodexInferenceEventType.PROCESS_ERROR)
        self.assertEqual(
            ctx.exception.event_type.value,
            CodexInferenceEventType.PROCESS_EXIT.value,
        )

    def test_l_cancellation(self):
        svc = _bare_service()

        def _cancel():
            for _ in range(80):
                if svc._active_turn_id:
                    break
                time.sleep(0.02)
            with svc._state_lock:
                svc._terminal = CodexInferenceEvent(
                    type=CodexInferenceEventType.CANCELLED,
                    thread_id="th-1",
                    turn_id=svc._active_turn_id,
                    message="Turn cancelled",
                )
                svc._terminal_event.set()

        threading.Thread(target=_cancel, daemon=True).start()
        with mock.patch.object(svc, "_assert_can_infer"):
            result = svc.run_turn("th-1", "x", idle_timeout_s=5.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, CodexInferenceEventType.CANCELLED.value)
        self.assertEqual(svc._watchdog_generation, 0)

    def test_n_cleanup_resets_watchdog(self):
        svc = _bare_service()
        with mock.patch.object(svc, "_assert_can_infer"):
            with self.assertRaises(CodexInferenceError):
                svc.run_turn("th-1", "stall", idle_timeout_s=0.25)
        self.assertEqual(svc._watchdog_generation, 0)
        self.assertIsNone(svc._last_activity_type)
        # Next turn starts clean.
        def _complete_fast():
            for _ in range(80):
                if svc._active_turn_id:
                    break
                time.sleep(0.02)
            with svc._state_lock:
                svc._terminal = CodexInferenceEvent(
                    type=CodexInferenceEventType.COMPLETED,
                    thread_id="th-1",
                    turn_id=svc._active_turn_id,
                )
                svc._terminal_event.set()

        threading.Thread(target=_complete_fast, daemon=True).start()
        with mock.patch.object(svc, "_assert_can_infer"):
            result = svc.run_turn("th-1", "next", idle_timeout_s=5.0)
        self.assertTrue(result.ok)


class CodexTimeoutUiGuardsTest(unittest.TestCase):
    def test_o_js_auth_cta_independent_of_timeouts(self):
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("function _codexAuthState", js)
        self.assertIn("function _codexSignInCtaReason", js)
        self.assertIn('if (_codexAuthState() !== "signed_out") return null;', js)
        self.assertIn("set-codex-idle-timeout", js)
        self.assertIn("set-codex-max-duration", js)
        self.assertNotIn("set-codex-turn-timeout", js)

    def test_default_settings(self):
        import bridge
        self.assertEqual(bridge.DEFAULT_SETTINGS.get("codex_idle_timeout_seconds"), 300)
        self.assertEqual(bridge.DEFAULT_SETTINGS.get("codex_max_task_duration_seconds"), 0)
        self.assertNotIn("codex_turn_timeout_seconds", bridge.DEFAULT_SETTINGS)

    def test_html_exposes_three_concepts(self):
        html = (Path(__file__).resolve().parent.parent / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Codex idle timeout", html)
        self.assertIn("Maximum task duration", html)
        self.assertIn("Approval timeout", html)
        self.assertIn("no progress or protocol activity", html)
        self.assertIn("Optional absolute safety limit", html)
        self.assertIn("90 seconds", html)
        self.assertNotIn("Codex task timeout", html)

    def test_provider_passes_idle_and_max(self):
        from providers import codex_provider as cp
        self.assertTrue(hasattr(cp, "get_codex_idle_timeout_seconds"))
        self.assertTrue(hasattr(cp, "get_codex_max_task_duration_seconds"))


if __name__ == "__main__":
    unittest.main()

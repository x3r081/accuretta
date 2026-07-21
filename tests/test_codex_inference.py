"""Codex inference service tests (fake subprocess — no network, no UI)."""

from __future__ import annotations

import json
import logging
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
from codex.flags import ENV_CODEX_INFERENCE_ENABLED, is_codex_inference_enabled
from codex.inference import CodexInferenceService
from codex.inference_types import CodexInferenceError, CodexInferenceEventType
from codex.session import CodexSession, reset_codex_session

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


class CodexInferenceFlagTest(unittest.TestCase):
    def test_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_CODEX_INFERENCE_ENABLED, None)
            self.assertFalse(is_codex_inference_enabled())

    def test_enabled_when_true(self):
        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "true"}):
            self.assertTrue(is_codex_inference_enabled())


class CodexInferenceServiceTest(unittest.TestCase):
    def setUp(self):
        reset_codex_session()
        reset_discovery_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = _make_fake_codex_bin(Path(self.tmp.name), mode="authenticated")
        self._env = mock.patch.dict(
            os.environ,
            {
                "ACCURETTA_CODEX_BIN": str(self.bin),
                ENV_CODEX_INFERENCE_ENABLED: "1",
                "FAKE_CODEX_TURN_DELAY_MS": "30",
            },
        )
        self._env.start()
        self.session = CodexSession(executable=str(self.bin))
        self.svc = CodexInferenceService(session=self.session)

    def tearDown(self):
        try:
            self.session.shutdown()
        except Exception:
            pass
        self._env.stop()
        os.environ.pop("FAKE_CODEX_EMIT_STALE_TURN", None)
        reset_codex_session()
        reset_discovery_cache()
        self.tmp.cleanup()

    def test_inference_disabled(self):
        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            avail = self.svc.check_available(live=False)
            self.assertFalse(avail.flag_enabled)
            self.assertFalse(avail.available)
            with self.assertRaises(CodexInferenceError) as ctx:
                self.svc.create_thread()
            self.assertEqual(ctx.exception.event_type, CodexInferenceEventType.UNAVAILABLE)

    def test_authenticated_success_stream_and_complete(self):
        avail = self.svc.check_available(live=True)
        self.assertTrue(avail.flag_enabled)
        self.assertTrue(avail.authenticated)
        self.assertTrue(avail.available)
        thread_id = self.svc.create_thread(model="fake-model")
        self.assertTrue(thread_id)
        result = self.svc.run_turn(thread_id, "hi", timeout_s=10)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.text, "Hello from Codex")
        types = [e.type for e in result.events]
        self.assertIn(CodexInferenceEventType.STARTED, types)
        self.assertIn(CodexInferenceEventType.TEXT_DELTA, types)
        self.assertIn(CodexInferenceEventType.COMPLETED, types)
        blob = json.dumps(result.to_safe_dict())
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("access_token", blob)
        self.assertNotIn("accessToken", blob)

    def test_unauthenticated_rejected(self):
        bin_unauth = _make_fake_codex_bin(Path(self.tmp.name) / "u", mode="ok")
        session = CodexSession(executable=str(bin_unauth))
        svc = CodexInferenceService(session=session)
        try:
            with self.assertRaises(CodexInferenceError) as ctx:
                svc.create_thread()
            self.assertEqual(
                ctx.exception.event_type,
                CodexInferenceEventType.AUTHENTICATION_REQUIRED,
            )
        finally:
            session.shutdown()

    def test_cancellation(self):
        os.environ["FAKE_CODEX_TURN_DELAY_MS"] = "500"
        thread_id = self.svc.create_thread()
        result_box = {}

        def _run():
            result_box["r"] = self.svc.run_turn(thread_id, "slow", timeout_s=10)

        th = threading.Thread(target=_run)
        th.start()
        time.sleep(0.2)
        cancelled = self.svc.cancel_active_turn()
        self.assertTrue(cancelled)
        th.join(timeout=5)
        self.assertFalse(th.is_alive())
        result = result_box["r"]
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(result.ok)

    def test_process_failure_during_turn(self):
        crash_bin = _make_fake_codex_bin(Path(self.tmp.name) / "crash", mode="crash_on_turn")
        session = CodexSession(executable=str(crash_bin))
        svc = CodexInferenceService(session=session)
        try:
            thread_id = svc.create_thread()
            with self.assertRaises(CodexInferenceError) as ctx:
                svc.run_turn(thread_id, "boom", timeout_s=5)
            self.assertEqual(ctx.exception.event_type, CodexInferenceEventType.PROCESS_ERROR)
        finally:
            session.shutdown()

    def test_stale_turn_ignored(self):
        os.environ["FAKE_CODEX_EMIT_STALE_TURN"] = "1"
        thread_id = self.svc.create_thread()
        result = self.svc.run_turn(thread_id, "hi", timeout_s=10)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Hello from Codex")
        self.assertNotIn("STALE", result.text)

    def test_malformed_notification_ignored(self):
        thread_id = self.svc.create_thread()
        result = self.svc.run_turn(thread_id, "hi", timeout_s=10)
        self.assertTrue(result.ok)

    def test_no_token_logged_or_persisted(self):
        records = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = _H()
        handler.setFormatter(logging.Formatter("%(message)s"))
        loggers = [
            logging.getLogger("accuretta.codex.inference"),
            logging.getLogger("accuretta.codex.rpc"),
            logging.getLogger("accuretta.codex.process"),
            logging.getLogger("accuretta.codex.session"),
        ]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        try:
            thread_id = self.svc.create_thread()
            result = self.svc.run_turn(thread_id, "hi", timeout_s=10)
            self.assertTrue(result.ok)
        finally:
            for lg in loggers:
                lg.removeHandler(handler)
        joined = "\n".join(records)
        self.assertNotIn(SECRET, joined)
        # AuthStore / settings not used — result DTO must stay clean.
        self.assertNotIn(SECRET, json.dumps(result.to_safe_dict()))
        # Auth still works with inference flag off.
        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            ctrl = self.session.ensure_ready()
            self.assertTrue(ctrl.account.authenticated)


if __name__ == "__main__":
    unittest.main()

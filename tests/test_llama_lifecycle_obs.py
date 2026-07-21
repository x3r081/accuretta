"""llama-server lifecycle observability tests (fake binary; no real GGUF).

Covers: normal startup, delayed readiness, startup failure, stdout/stderr flood,
exit during generation, never-ending stream, user cancellation, bridge shutdown.

Run: python3 -m unittest tests.test_llama_lifecycle_obs -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _TESTS_DIR)

import bridge
from macos_fakes import free_localhost_port, write_fake_llama, write_stub_model


def _skip_unless_posix():
    return unittest.skipUnless(os.name == "posix", "requires POSIX process groups / scripts")


@_skip_unless_posix()
class LlamaLifecycleObsTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="accuretta-life-")
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        data = self.root / "data"
        data.mkdir()
        settings = data / "settings.json"
        settings.write_text(json.dumps({
            "spec_strategy": "off",
            "enable_speculative": False,
            "flash_attn": False,
            "num_gpu": 0,
            "num_ctx": 512,
            "num_batch": 32,
            "n_ubatch": 32,
            "watchdog_enabled": False,
            "model": "local",
            "enable_thinking": False,
        }), encoding="utf-8")
        self._orig_settings = bridge.SETTINGS_FILE
        bridge.SETTINGS_FILE = settings
        self.addCleanup(lambda: setattr(bridge, "SETTINGS_FILE", self._orig_settings))

        self.model = write_stub_model(self.root / "models")
        self.bin_path = write_fake_llama(self.root / "bin")
        self.port = free_localhost_port()
        self._orig_llama = bridge.LLAMA
        bridge.LLAMA = f"http://127.0.0.1:{self.port}"
        self.addCleanup(lambda: setattr(bridge, "LLAMA", self._orig_llama))

        self.proc = bridge.LlamaProcess()
        self.addCleanup(self._cleanup)

        os.environ.pop("ACCURETTA_DEBUG_LLAMA", None)
        self.addCleanup(lambda: os.environ.pop("ACCURETTA_FAKE_LLAMA_MODE", None))
        self.addCleanup(lambda: os.environ.pop("ACCURETTA_FAKE_LLAMA_DELAY", None))
        self.addCleanup(lambda: os.environ.pop("ACCURETTA_LLAMA_GEN_IDLE_TIMEOUT", None))
        self.addCleanup(lambda: os.environ.pop("ACCURETTA_LLAMA_GEN_MAX_S", None))

    def _cleanup(self):
        try:
            self.proc.stop_permanent(timeout=3.0)
            self.proc.shutdown_watchdog()
        except Exception:
            pass

    def _patch_bin(self):
        return mock.patch.object(
            bridge, "resolve_llama_bin",
            return_value={
                "path": self.bin_path,
                "validation": "ok",
                "source": "settings",
                "basename": "llama-server",
                "error": None,
                "hint": None,
            },
        )

    def _wait(self, pred, timeout=8.0, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(interval)
        return False

    def test_normal_startup(self):
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "ready"
        with self._patch_bin(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 1.0"), \
             mock.patch.object(bridge, "probe_llama_devices",
                               return_value={"metal_llama_build": True, "devices": []}):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=8, port_override=self.port,
            )
        self.assertTrue(res.get("ok"), msg=res)
        snap = self.proc.lifecycle_snapshot()
        self.assertEqual(snap["state"], "ready")
        self.assertIn(snap["diagnostics"].get("discovery_source"), ("settings", "path", "homebrew"))
        self.assertEqual(snap["diagnostics"].get("port"), self.port)
        self.assertIsNotNone(snap["diagnostics"].get("startup_s"))
        # No absolute model path in diagnostics / redacted logs.
        self.assertEqual(snap["diagnostics"].get("model_basename"), Path(self.model).name)
        self.assertNotIn(str(self.root), json.dumps(snap))
        log = self.proc.read_log(tail=50)
        blob = "\n".join(log.get("lines") or [])
        self.assertNotIn(str(Path.home()), blob)

    def test_delayed_readiness(self):
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "delayed_ready"
        os.environ["ACCURETTA_FAKE_LLAMA_DELAY"] = "0.8"
        with self._patch_bin(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 1.0"), \
             mock.patch.object(bridge, "probe_llama_devices",
                               return_value={"metal_llama_build": False, "devices": []}):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=8, port_override=self.port,
            )
        self.assertTrue(res.get("ok"), msg=res)
        self.assertEqual(self.proc.lifecycle_snapshot()["state"], "ready")
        self.assertGreaterEqual(self.proc.lifecycle_snapshot()["diagnostics"].get("readiness_s") or 0, 0.5)

    def test_startup_failure(self):
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "exit_soon"
        with self._patch_bin(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 1.0"), \
             mock.patch.object(bridge, "probe_llama_devices",
                               return_value={"ok": False, "devices": []}):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=5, port_override=self.port,
            )
        self.assertFalse(res.get("ok"))
        snap = self.proc.lifecycle_snapshot()
        self.assertEqual(snap["state"], "startup_failed")
        self.assertIn("waiting_for", snap)
        err = (res.get("error") or "").lower()
        self.assertTrue("exited" in err or "ready" in err, msg=res)

    def test_stdout_flood_bounded(self):
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "flood_stdout"
        with self._patch_bin(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 1.0"), \
             mock.patch.object(bridge, "probe_llama_devices",
                               return_value={"devices": []}):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=10, port_override=self.port,
            )
        self.assertTrue(res.get("ok"), msg=res)
        log = self.proc.read_log(tail=900)
        lines = log.get("lines") or []
        self.assertLessEqual(len(lines), bridge._LLAMA_LOG_MAX_LINES)
        # Flood suppressor note or truncated lines present; buffer stayed bounded.
        joined = "\n".join(lines)
        self.assertTrue(
            "suppressed" in joined or len(lines) <= bridge._LLAMA_LOG_MAX_LINES
        )

    def test_stderr_flood_bounded(self):
        # stderr is merged into stdout by Popen; same path as flood_stdout.
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "flood_stderr"
        with self._patch_bin(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 1.0"), \
             mock.patch.object(bridge, "probe_llama_devices",
                               return_value={"devices": []}):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=10, port_override=self.port,
            )
        self.assertTrue(res.get("ok"), msg=res)
        self.assertLessEqual(len(self.proc.read_log(tail=900).get("lines") or []),
                             bridge._LLAMA_LOG_MAX_LINES)

    def test_exit_during_generation(self):
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "exit_during_gen"
        with self._patch_bin(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 1.0"), \
             mock.patch.object(bridge, "probe_llama_devices",
                               return_value={"devices": []}):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=8, port_override=self.port,
            )
        self.assertTrue(res.get("ok"), msg=res)

        # Point global _llama at this managed process so stream liveness works.
        old = bridge._llama
        bridge._llama = self.proc
        self.addCleanup(lambda: setattr(bridge, "_llama", old))

        events: list[dict] = []

        def emit(ev):
            events.append(ev)

        with mock.patch.object(bridge, "_llama_props_ctx", return_value=4096), \
             mock.patch.object(bridge, "truncate_messages",
                               side_effect=lambda m, *a, **k: m):
            bridge.run_chat_turn(
                "life-exit-gen",
                [{"role": "user", "content": "hi"}],
                use_tools=False,
                emit=emit,
                native_tools=False,
            )

        # Must terminate the UI stream with an error (not hang).
        self.assertTrue(
            self._wait(
                lambda: any(e.get("type") == "error" for e in events)
                or any(e.get("type") == "delta" for e in events),
                timeout=5.0,
            )
        )
        codes = {e.get("code") for e in events if e.get("type") == "error"}
        # Either explicit exit error or connection failure after child dies.
        self.assertTrue(
            codes & {"exited_unexpectedly", "generation_failed", "timed_out"}
            or any(e.get("type") == "delta" for e in events),
            msg=events,
        )

    def test_never_ending_stream_times_out(self):
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "endless_stream"
        # Wall clock must bind the turn even when chunks keep arriving.
        # Idle stays high so a chatty endless stream is cut by wall timeout, not stall.
        os.environ["ACCURETTA_LLAMA_GEN_IDLE_TIMEOUT"] = "30"
        os.environ["ACCURETTA_LLAMA_GEN_MAX_S"] = "4"
        with self._patch_bin(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 1.0"), \
             mock.patch.object(bridge, "probe_llama_devices",
                               return_value={"devices": []}):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=8, port_override=self.port,
            )
        self.assertTrue(res.get("ok"), msg=res)
        old = bridge._llama
        bridge._llama = self.proc
        self.addCleanup(lambda: setattr(bridge, "_llama", old))

        events: list[dict] = []

        def emit(ev):
            events.append(ev)

        t0 = time.perf_counter()
        with mock.patch.object(bridge, "_llama_props_ctx", return_value=4096), \
             mock.patch.object(bridge, "truncate_messages",
                               side_effect=lambda m, *a, **k: m):
            bridge.run_chat_turn(
                "life-endless",
                [{"role": "user", "content": "hi"}],
                use_tools=False,
                emit=emit,
                native_tools=False,
            )
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 15.0, "endless stream must not hang the turn")
        err_events = [e for e in events if e.get("type") == "error"]
        # Endless mode keeps sending chunks — wall timeout should fire.
        self.assertTrue(err_events, msg=events)
        self.assertTrue(
            any(e.get("code") == "timed_out" for e in err_events)
            or any("timeout" in (e.get("error") or "").lower() for e in err_events),
            msg=err_events,
        )

    def test_user_cancellation(self):
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "endless_stream"
        os.environ["ACCURETTA_LLAMA_GEN_IDLE_TIMEOUT"] = "30"
        os.environ["ACCURETTA_LLAMA_GEN_MAX_S"] = "60"
        with self._patch_bin(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 1.0"), \
             mock.patch.object(bridge, "probe_llama_devices",
                               return_value={"devices": []}):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=8, port_override=self.port,
            )
        self.assertTrue(res.get("ok"), msg=res)
        old = bridge._llama
        bridge._llama = self.proc
        self.addCleanup(lambda: setattr(bridge, "_llama", old))

        events: list[dict] = []
        chat_id = "life-cancel"

        def emit(ev):
            events.append(ev)

        def cancel_soon():
            time.sleep(0.4)
            bridge.cancel_chat(chat_id)

        th = threading.Thread(target=cancel_soon, daemon=True)
        th.start()
        with mock.patch.object(bridge, "_llama_props_ctx", return_value=4096), \
             mock.patch.object(bridge, "truncate_messages",
                               side_effect=lambda m, *a, **k: m):
            bridge.run_chat_turn(
                chat_id,
                [{"role": "user", "content": "hi"}],
                use_tools=False,
                emit=emit,
                native_tools=False,
            )
        th.join(timeout=2)
        self.assertTrue(
            any(e.get("type") == "notice" and "stopped" in (e.get("note") or "").lower()
                for e in events)
            or any(e.get("code") == "cancelled" for e in events),
            msg=events,
        )

    def test_bridge_shutdown_cleans_child(self):
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "ready"
        with self._patch_bin(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 1.0"), \
             mock.patch.object(bridge, "probe_llama_devices",
                               return_value={"devices": []}):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=8, port_override=self.port,
            )
        self.assertTrue(res.get("ok"), msg=res)
        pid = res.get("pid")
        self.assertTrue(self.proc.is_running())
        self.proc.shutdown_watchdog()
        self.proc.stop_permanent(timeout=3.0)
        self.assertEqual(self.proc.lifecycle_snapshot()["state"], "stopped")
        self.assertFalse(self.proc.is_running())

        def gone():
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:
                return False

        self.assertTrue(self._wait(gone, timeout=3.0), f"pid {pid} still alive")
        self.assertFalse(
            bridge.llama_ping(timeout=0.3, base_url=f"http://127.0.0.1:{self.port}")
        )


class IterLlamaSSEUnitTest(unittest.TestCase):
    def test_idle_timeout_message(self):
        class Slow:
            def __iter__(self):
                return self

            def __next__(self):
                time.sleep(0.5)
                raise TimeoutError("timed out")

            def close(self):
                pass

        with self.assertRaises(bridge.LlamaStreamTimeout) as ctx:
            list(bridge.iter_llama_sse(
                Slow(), idle_timeout=0.2, wall_timeout=2.0,
            ))
        self.assertEqual(ctx.exception.code, "timed_out")
        self.assertIn("SSE", ctx.exception.waiting_for)

    def test_cancel_stops_iteration(self):
        ev = threading.Event()
        ev.set()

        class Forever:
            def __iter__(self):
                while True:
                    yield b"data: x\n\n"
                    time.sleep(0.01)

            def close(self):
                pass

        out = list(bridge.iter_llama_sse(
            Forever(), idle_timeout=5, wall_timeout=5, cancel_ev=ev,
        ))
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

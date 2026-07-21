"""End-to-end macOS acceptance harness (fakes + temp dirs; no real GGUF/network).

Run:
  python3 -m unittest tests.test_macos_acceptance -v
  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _TESTS_DIR)

import bridge
from macos_fakes import (
    MTL_LIST,
    LocalModelsServer,
    apple_sysctl,
    free_localhost_port,
    make_stream_iter,
    sse_bytes,
    write_fake_llama,
    write_stub_model,
)


def _skip_unless_posix(reason: str = "requires POSIX executable scripts"):
    return unittest.skipUnless(os.name == "posix", reason)


class _TempSettingsMixin:
    """Redirect SETTINGS_FILE into a TemporaryDirectory; never touch repo data/."""

    def _enter_temp_settings(self, extra: dict | None = None) -> Path:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="accuretta-macos-acc-")
        self.addCleanup(self._tmpdir.cleanup)
        root = Path(self._tmpdir.name)
        data = root / "data"
        data.mkdir()
        settings_path = data / "settings.json"
        payload = {
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
        }
        if extra:
            payload.update(extra)
        settings_path.write_text(json.dumps(payload), encoding="utf-8")
        self._orig_settings_file = bridge.SETTINGS_FILE
        bridge.SETTINGS_FILE = settings_path
        self.addCleanup(self._restore_settings_file)
        return root

    def _restore_settings_file(self):
        bridge.SETTINGS_FILE = self._orig_settings_file


# ---------------------------------------------------------------------------
# 1. First startup on Darwin arm64
# ---------------------------------------------------------------------------


class FirstStartupDarwinTest(unittest.TestCase):
    def tearDown(self):
        bridge._HW_SPECS_CACHE = None
        bridge._METAL_RUNTIME_SELECTED = False
        os.environ.pop("ACCURETTA_LLAMA_BIN", None)

    def test_first_startup_darwin_arm64_capabilities(self):
        from ui_copy import attach_ui_capabilities, assert_platform_copy_sanitized

        info = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values=apple_sysctl(24),
            list_devices_text=MTL_LIST,
            use_cache=False,
        )
        caps = attach_ui_capabilities(info)
        self.assertEqual(caps["os"], "macos")
        self.assertTrue(caps["is_apple_silicon"])
        self.assertEqual(caps["architecture"], "arm64")
        self.assertFalse(caps["features"]["sandbox_wsl"])
        self.assertFalse(caps["features"]["llama_one_click_windows"])
        self.assertTrue(caps["features"]["metal_guidance"])
        self.assertTrue(caps["features"]["homebrew_paths"])
        assert_platform_copy_sanitized("macos", caps["copy"])
        # Empty discovery (first run) surfaces actionable Homebrew guidance.
        with mock.patch.object(bridge, "_well_known_llama_paths", return_value=[]):
            missing = bridge.resolve_llama_bin(
                settings_bin="",
                env_bin="",
                which=lambda n: None,
                platform_name="darwin",
                machine="arm64",
                probe_version=False,
                skip_scan=True,
            )
        self.assertNotEqual(missing["validation"], "ok")
        self.assertIn("Homebrew", missing.get("hint") or "")


# ---------------------------------------------------------------------------
# 2–5. llama-server discovery
# ---------------------------------------------------------------------------


class LlamaDiscoveryAcceptanceTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ACCURETTA_LLAMA_BIN", None)

    def test_llama_server_found_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_path = write_fake_llama(Path(tmp) / "pathbin")

            def fake_which(name):
                return bin_path if name == "llama-server" else None

            info = bridge.resolve_llama_bin(
                settings_bin="",
                env_bin="",
                which=fake_which,
                platform_name="darwin",
                machine="arm64",
                probe_version=False,
                skip_scan=True,
            )
            self.assertEqual(info["validation"], "ok")
            self.assertEqual(info["source"], "path")
            self.assertEqual(info["path"], bin_path)

    def test_llama_server_found_in_opt_homebrew_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            brew = write_fake_llama(Path(tmp) / "opt" / "homebrew" / "bin")
            # Candidate list must include the real Homebrew prefix path.
            known = bridge._well_known_llama_paths("darwin")
            paths = [p for p, src in known]
            self.assertIn("/opt/homebrew/bin/llama-server", paths)
            self.assertEqual(
                next(src for p, src in known if p == "/opt/homebrew/bin/llama-server"),
                "homebrew",
            )
            with mock.patch.object(
                bridge, "_well_known_llama_paths",
                return_value=[(brew, "homebrew")],
            ):
                info = bridge.resolve_llama_bin(
                    settings_bin="",
                    env_bin="",
                    which=lambda n: None,
                    platform_name="darwin",
                    machine="arm64",
                    probe_version=False,
                    skip_scan=True,
                )
            self.assertEqual(info["source"], "homebrew")
            self.assertEqual(info["path"], brew)
            self.assertEqual(info["validation"], "ok")

    def test_explicit_config_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            configured = write_fake_llama(Path(tmp) / "configured")
            path_hit = write_fake_llama(Path(tmp) / "on-path")
            brew = write_fake_llama(Path(tmp) / "opt" / "homebrew" / "bin")

            def fake_which(name):
                return path_hit if name == "llama-server" else None

            with mock.patch.object(
                bridge, "_well_known_llama_paths",
                return_value=[(brew, "homebrew")],
            ):
                info = bridge.resolve_llama_bin(
                    settings_bin=configured,
                    env_bin="",
                    which=fake_which,
                    platform_name="darwin",
                    machine="arm64",
                    probe_version=False,
                    skip_scan=True,
                )
            self.assertEqual(info["source"], "settings")
            self.assertEqual(info["path"], configured)

    def test_invalid_configured_executable_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "gone" / "llama-server")
            brew = write_fake_llama(Path(tmp) / "opt" / "homebrew" / "bin")
            with mock.patch.object(
                bridge, "_well_known_llama_paths",
                return_value=[(brew, "homebrew")],
            ):
                info = bridge.resolve_llama_bin(
                    settings_bin=missing,
                    env_bin="",
                    which=lambda n: brew,
                    platform_name="darwin",
                    machine="arm64",
                    probe_version=False,
                    skip_scan=True,
                )
            self.assertEqual(info["source"], "settings")
            self.assertEqual(info["validation"], "missing")
            self.assertNotEqual(info["path"], brew)
            msg = bridge._llama_bin_error_message(info)
            self.assertIn("Configured", msg)
            self.assertIn("not found", msg.lower())
            self.assertIn("Homebrew", msg)


# ---------------------------------------------------------------------------
# 6. Windows zip download refused on Darwin
# ---------------------------------------------------------------------------


class DownloadGateAcceptanceTest(unittest.TestCase):
    def test_windows_zip_endpoint_refuses_on_darwin(self):
        self.assertFalse(bridge.windows_llama_zip_download_allowed("darwin"))
        with mock.patch("urllib.request.urlopen") as urlopen:
            bridge.download_and_extract_llama("CPU")
            urlopen.assert_not_called()
        with bridge.DOWNLOAD_LOCK:
            self.assertEqual(bridge.DOWNLOAD_STATE.get("status"), "failed")
            err = (bridge.DOWNLOAD_STATE.get("error_msg") or "").lower()
            self.assertIn("not available", err)


# ---------------------------------------------------------------------------
# 7. Folder picker matrix
# ---------------------------------------------------------------------------


class FolderPickerAcceptanceTest(unittest.TestCase):
    def tearDown(self):
        bridge._pick_folder_impl = None
        os.environ.pop("ACCURETTA_NO_GUI", None)

    def test_picker_success(self):
        bridge._pick_folder_impl = lambda title: bridge._picker_ok("/tmp/accuretta-models")
        r = bridge.pick_folder("Pick models folder")
        self.assertEqual(r["path"], "/tmp/accuretta-models")
        self.assertFalse(r["cancelled"])

    def test_picker_cancellation(self):
        bridge._pick_folder_impl = lambda title: bridge._picker_cancel()
        r = bridge.pick_folder()
        self.assertTrue(r["cancelled"])
        self.assertEqual(r["path"], "")

    def test_picker_unavailable_native_dialog(self):
        os.environ["ACCURETTA_NO_GUI"] = "1"
        r = bridge.pick_folder()
        self.assertIn(r["code"], ("no_gui", "unavailable"))
        self.assertIn("Paste the folder path", r["message"] or "")

    def test_picker_native_failure(self):
        def boom(_t):
            raise RuntimeError("NSOpenPanel failed\nTraceback (most recent call last):")

        bridge._pick_folder_impl = boom
        r = bridge.pick_folder()
        self.assertEqual(r["code"], "picker_failed")
        self.assertNotIn("Traceback", r["error"] or "")
        self.assertNotIn("Traceback", r["message"] or "")

    def test_picker_path_with_spaces(self):
        fake = subprocess.CompletedProcess(
            args=["osascript"], returncode=0,
            stdout="/Users/me/My Models/GGUF Folder/\n", stderr="",
        )
        with mock.patch.object(bridge.sys, "platform", "darwin"), \
             mock.patch.object(bridge.subprocess, "run", return_value=fake):
            r = bridge.pick_folder("Pick models folder")
        self.assertEqual(r["path"], "/Users/me/My Models/GGUF Folder")
        self.assertFalse(r["cancelled"])

    def test_picker_unicode_path(self):
        fake = subprocess.CompletedProcess(
            args=["osascript"], returncode=0,
            stdout="/Users/me/模型/テスト フォルダ/\n", stderr="",
        )
        with mock.patch.object(bridge.sys, "platform", "darwin"), \
             mock.patch.object(bridge.subprocess, "run", return_value=fake):
            r = bridge.pick_folder("Pick models folder")
        self.assertEqual(r["path"], "/Users/me/模型/テスト フォルダ")
        self.assertFalse(r["cancelled"])

    def test_picker_bridge_remains_alive_afterward(self):
        """Picker failure must not leave the process unusable for a second pick."""
        calls = {"n": 0}

        def flaky(title):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient AppKit failure")
            return bridge._picker_ok("/Users/me/Models")

        bridge._pick_folder_impl = flaky
        first = bridge.pick_folder()
        self.assertEqual(first["code"], "picker_failed")
        second = bridge.pick_folder()
        self.assertEqual(second["path"], "/Users/me/Models")
        self.assertIsNone(second["error"])
        # A third call still works (bridge "alive").
        third = bridge.pick_folder()
        self.assertEqual(third["path"], "/Users/me/Models")


# ---------------------------------------------------------------------------
# 8–9. Apple Silicon + unified-memory bands
# ---------------------------------------------------------------------------


class AppleSiliconAcceptanceTest(unittest.TestCase):
    def tearDown(self):
        bridge._HW_SPECS_CACHE = None
        bridge._METAL_RUNTIME_SELECTED = False

    def test_apple_silicon_system_information(self):
        info = bridge.detect_hardware_specs(
            platform_name="darwin",
            machine="arm64",
            sysctl_values=apple_sysctl(24, "Apple M4 Pro"),
            list_devices_text=MTL_LIST,
            use_cache=False,
        )
        self.assertTrue(info["is_apple_silicon"])
        self.assertEqual(info["chip_name"], "Apple M4 Pro")
        self.assertEqual(info["memory_model"], "unified")
        self.assertEqual(info["os"], "macos")
        self.assertNotEqual(info["gpu_name"], "Generic CPU")
        self.assertTrue(info["metal_hardware"])
        self.assertTrue(info["metal_llama_build"])
        self.assertEqual(info["recommended_num_gpu"], 99)

    def test_unified_memory_recommendations_16_24_32_48_64(self):
        # (total_gb, reserve, usable, model_class_substring, ctx_label)
        bands = [
            (16, 6.5, 9.5, "3B–8B", "4K–8K"),
            (24, 8.0, 16.0, "7B–14B", "8K–16K"),
            (32, 8.5, 23.5, "14B–32B", "16K–32K"),
            (48, 11.0, 37.0, "32B–70B", "32K–65K"),
            (64, 13.0, 51.0, "70B", "32K–131K"),
        ]
        for total, reserve, usable, klass, ctx in bands:
            with self.subTest(total_gb=total):
                # Skip device free tightening so reserve math is deterministic.
                info = bridge.detect_hardware_specs(
                    platform_name="darwin",
                    machine="arm64",
                    sysctl_values=apple_sysctl(total),
                    list_devices_text="BLAS: Accelerate (0 MiB, 0 MiB free)\n",
                    use_cache=False,
                )
                self.assertEqual(info["unified_memory_gb"], float(total))
                self.assertEqual(info["memory_reserve_gb"], reserve)
                self.assertAlmostEqual(info["usable_memory_gb"], usable, places=1)
                self.assertIn(klass, info["recommended_model_class"])
                self.assertEqual(info["recommended_context_label"], ctx)
                self.assertEqual(info["memory_model"], "unified")


# ---------------------------------------------------------------------------
# 10. WSL unsupported on Darwin
# ---------------------------------------------------------------------------


class WslSandboxAcceptanceTest(unittest.TestCase):
    def test_wsl_sandbox_unsupported_platform_on_darwin(self):
        with mock.patch("ui_copy.normalize_os", return_value="macos"), \
             mock.patch.object(bridge, "_wsl_run") as wsl_run:
            info = bridge.wsl_probe()
        wsl_run.assert_not_called()
        self.assertEqual(info["state"], "unsupported_platform")
        self.assertTrue(info.get("unsupported_platform"))
        self.assertFalse(info["ready"])
        self.assertFalse(info["wsl_installed"])


# ---------------------------------------------------------------------------
# 11–12. Explicit gaps (skipped until product catches up)
# ---------------------------------------------------------------------------


class PosixShellGapTest(unittest.TestCase):
    @unittest.skip(
        "POSIX agent shell not implemented: TOOL_ALIASES maps bash/shell/cmd "
        "→ run_powershell (PowerShell). Manual validation still required."
    )
    def test_shell_aliases_execute_posix_shell(self):
        self.assertEqual(bridge._resolve_tool_name("bash"), "run_shell")
        self.assertEqual(bridge._resolve_tool_name("shell"), "run_shell")


class ProtectedMacOSPathsTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS protected paths")
    def test_protected_macos_paths_rejected(self):
        for p in ("/System/Library", "/Library/Preferences",
                  os.path.expanduser("~/.ssh"), os.path.expanduser("~/.aws")):
            self.assertTrue(bridge.is_blocked_path(p), p)


# ---------------------------------------------------------------------------
# 13–17. llama-server lifecycle (fake executable + temp dirs)
# ---------------------------------------------------------------------------


@_skip_unless_posix()
class LlamaLifecycleAcceptanceTest(_TempSettingsMixin, unittest.TestCase):
    def setUp(self):
        self.root = self._enter_temp_settings()
        self.model = write_stub_model(self.root / "models")
        self.bin_path = write_fake_llama(self.root / "bin")
        self.port = free_localhost_port()
        self._orig_llama = bridge.LLAMA
        bridge.LLAMA = f"http://127.0.0.1:{self.port}"
        self.addCleanup(self._restore_llama)
        self.proc = bridge.LlamaProcess()
        self.addCleanup(self._cleanup_proc)
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "ready"
        self.addCleanup(lambda: os.environ.pop("ACCURETTA_FAKE_LLAMA_MODE", None))

    def _restore_llama(self):
        bridge.LLAMA = self._orig_llama

    def _cleanup_proc(self):
        try:
            self.proc.stop_permanent(timeout=3.0)
            self.proc.shutdown_watchdog()
        except Exception:
            pass

    def _patch_resolve(self):
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

    def _wait_until(self, pred, timeout=5.0, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(interval)
        return False

    def test_startup_timeout(self):
        os.environ["ACCURETTA_FAKE_LLAMA_MODE"] = "hang"
        with self._patch_resolve(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 0"):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=1, port_override=self.port,
            )
        self.assertFalse(res.get("ok"))
        err = (res.get("error") or "").lower()
        life = (res.get("lifecycle") or {}).get("state") or ""
        self.assertTrue(
            "didn't answer" in err
            or "exited" in err
            or life in ("timed_out", "startup_failed"),
            msg=res,
        )

    def test_shutdown_terminates_child_processes(self):
        with self._patch_resolve(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 0"):
            # wait=False avoids wait_for_llama's fixed sleeps; we poll readiness.
            res = self.proc.start(
                self.model, wait=False, wait_seconds=8, port_override=self.port,
            )
        self.assertTrue(res.get("ok"), msg=res)
        pid = res.get("pid")
        self.assertTrue(self.proc.is_running())
        self.assertTrue(
            self._wait_until(
                lambda: bridge.llama_ping(
                    timeout=0.2, base_url=f"http://127.0.0.1:{self.port}"
                ),
                timeout=5.0,
            ),
            "fake llama-server never became ready",
        )
        self.proc.stop_permanent(timeout=3.0)
        self.assertFalse(self.proc.is_running())
        self.assertTrue(
            self._wait_until(lambda: _pid_gone(pid), timeout=3.0),
            f"child pid {pid} still alive after stop_permanent",
        )

    def test_port_already_occupied(self):
        occupant = LocalModelsServer(port=self.port)
        occupant.start()
        self.addCleanup(occupant.stop)
        self.assertTrue(
            self._wait_until(
                lambda: bridge.llama_ping(
                    timeout=0.3, base_url=f"http://127.0.0.1:{self.port}"
                ),
                timeout=2.0,
            )
        )
        with self._patch_resolve(), \
             mock.patch.object(bridge, "probe_llama_version", return_value="fake 0"):
            res = self.proc.start(
                self.model, wait=True, wait_seconds=2, port_override=self.port,
            )
        self.assertFalse(res.get("ok"))
        self.assertIn("already in use", (res.get("error") or "").lower())


def _pid_gone(pid: int | None) -> bool:
    if not pid:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class StreamLifecycleAcceptanceTest(_TempSettingsMixin, unittest.TestCase):
    def setUp(self):
        self._enter_temp_settings({"model": "local-fake"})

    def _run_turn(self, stream_factory):
        events: list[dict] = []

        def emit(ev):
            events.append(ev)

        with mock.patch.object(bridge, "llama_post_stream", side_effect=stream_factory), \
             mock.patch.object(bridge, "_llama_props_ctx", return_value=4096), \
             mock.patch.object(
                 bridge, "truncate_messages",
                 side_effect=lambda msgs, *a, **k: msgs,
             ):
            result = bridge.run_chat_turn(
                "acc-stream-1",
                [{"role": "user", "content": "hi"}],
                use_tools=False,
                emit=emit,
                native_tools=False,
            )
        return events, result

    def test_llama_exits_during_generation(self):
        chunks = sse_bytes({"choices": [{"delta": {"content": "hel"}}]})
        factory = make_stream_iter(
            chunks, die_after=1, exc_factory=lambda: ConnectionResetError("reset")
        )
        events, result = self._run_turn(factory)
        deltas = "".join(e.get("content", "") for e in events if e.get("type") == "delta")
        self.assertIn("hel", deltas)
        # Outer handler returns a partial assistant message instead of crashing.
        self.assertIsInstance(result, dict)
        self.assertIn("hel", result.get("content") or "")

    def test_malformed_streaming_response(self):
        chunks = sse_bytes(
            "not-json{{{",
            {"choices": [{"delta": {"content": "ok"}}]},
            "[DONE]",
        )
        # Inject a raw malformed line plus a good frame.
        bad_and_good = [
            b"data: not-json{{{\n\n",
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        factory = make_stream_iter(bad_and_good)
        events, result = self._run_turn(factory)
        deltas = "".join(e.get("content", "") for e in events if e.get("type") == "delta")
        self.assertIn("ok", deltas)
        self.assertIsInstance(result, dict)
        self.assertIn("ok", result.get("content") or "")
        # No error event required for skipped malformed frames.
        self.assertFalse(any(e.get("type") == "error" for e in events))


# ---------------------------------------------------------------------------
# 18. Persisted Windows settings on macOS
# ---------------------------------------------------------------------------


class WindowsSettingsMigrationAcceptanceTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ACCURETTA_LLAMA_BIN", None)

    def test_windows_settings_safely_interpreted_on_macos(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "settings.json"
            win = {
                "llama_bin": r"C:\Users\Alice\AppData\Local\llama\llama-server.exe",
                "models_dir": r"D:\MODELS",
                "model": r"D:\MODELS\qwen.gguf",
                "spec_strategy": "ngram-mod",
                "enable_speculative": True,
            }
            settings_path.write_text(json.dumps(win), encoding="utf-8")
            with mock.patch.object(bridge, "SETTINGS_FILE", settings_path):
                s = bridge.get_settings()
                # Values are preserved (no crash / silent wipe).
                self.assertEqual(s["llama_bin"], win["llama_bin"])
                self.assertEqual(s["models_dir"], win["models_dir"])
                # Defaults still fill missing keys.
                self.assertIn("num_ctx", s)
                info = bridge.resolve_llama_bin(
                    settings_bin=s["llama_bin"],
                    env_bin="",
                    which=lambda n: None,
                    platform_name="darwin",
                    machine="arm64",
                    probe_version=False,
                    skip_scan=True,
                )
            self.assertEqual(info["source"], "settings")
            self.assertNotEqual(info["validation"], "ok")
            msg = bridge._llama_bin_error_message(info)
            self.assertIn("Configured", msg)
            # Basename stays platform-correct for hints (not forced to .exe on macOS).
            self.assertEqual(bridge.llama_server_basename("darwin"), "llama-server")
            from ui_copy import build_ui_capabilities
            caps = build_ui_capabilities(platform_name="darwin")
            self.assertFalse(caps["features"]["llama_one_click_windows"])
            self.assertFalse(caps["features"]["sandbox_wsl"])


# ---------------------------------------------------------------------------
# Timed runner helper (optional: python3 -m tests.test_macos_acceptance)
# ---------------------------------------------------------------------------


class TimedTextTestResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings: list[tuple[float, str]] = []

    def startTest(self, test):
        self._t0 = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        elapsed = time.perf_counter() - getattr(self, "_t0", time.perf_counter())
        self.timings.append((elapsed, str(test)))
        super().stopTest(test)


class TimedTextTestRunner(unittest.TextTestRunner):
    resultclass = TimedTextTestResult


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = TimedTextTestRunner(verbosity=2)
    result = runner.run(suite)
    if getattr(result, "timings", None):
        print("\nSlowest tests:")
        for elapsed, name in sorted(result.timings, reverse=True)[:10]:
            print(f"  {elapsed:6.3f}s  {name}")
    sys.exit(0 if result.wasSuccessful() else 1)

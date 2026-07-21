"""Tests for cross-platform llama-server discovery.

Run: python -m unittest tests.test_llama_discovery -v
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge


class LlamaDiscoveryTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("ACCURETTA_LLAMA_BIN", None)

    def _touch_exec(self, path: Path, executable: bool = True) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\necho stub\n", encoding="utf-8")
        mode = path.stat().st_mode
        if executable:
            path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else:
            path.chmod(mode & ~stat.S_IXUSR & ~stat.S_IXGRP & ~stat.S_IXOTH)
        return str(path.resolve())

    def test_configured_executable_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_path = self._touch_exec(Path(tmp) / "my llama" / "llama-server")
            info = bridge.resolve_llama_bin(
                settings_bin=bin_path,
                env_bin="",
                which=lambda n: None,
                platform_name="darwin",
                machine="arm64",
                probe_version=False,
                skip_scan=True,
            )
            self.assertEqual(info["validation"], "ok")
            self.assertEqual(info["source"], "settings")
            self.assertEqual(info["path"], bin_path)
            self.assertEqual(info["detected_platform"], "macos")
            self.assertEqual(info["architecture"], "arm64")
            self.assertEqual(info["basename"], "llama-server")

    def test_configured_not_silently_replaced(self):
        """Invalid settings path must not be swapped for Homebrew/PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            homebrew = self._touch_exec(Path(tmp) / "opt" / "homebrew" / "bin" / "llama-server")
            missing = str(Path(tmp) / "does-not-exist" / "llama-server")
            with mock.patch.object(bridge, "_well_known_llama_paths",
                                   return_value=[(homebrew, "homebrew")]):
                info = bridge.resolve_llama_bin(
                    settings_bin=missing,
                    env_bin="",
                    which=lambda n: homebrew,
                    platform_name="darwin",
                    machine="arm64",
                    probe_version=False,
                    skip_scan=True,
                )
            self.assertEqual(info["source"], "settings")
            self.assertEqual(info["validation"], "missing")
            self.assertNotEqual(info["path"], homebrew)
            self.assertIn("not found", (info["error"] or "").lower())

    def test_path_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_path = self._touch_exec(Path(tmp) / "bin" / "llama-server")

            def fake_which(name):
                if name == "llama-server":
                    return bin_path
                return None

            info = bridge.resolve_llama_bin(
                settings_bin="",
                env_bin="",
                which=fake_which,
                platform_name="linux",
                machine="x86_64",
                probe_version=False,
                skip_scan=True,
            )
            self.assertEqual(info["validation"], "ok")
            self.assertEqual(info["source"], "path")
            self.assertEqual(info["path"], bin_path)
            self.assertEqual(info["detected_platform"], "linux")

    def test_apple_silicon_homebrew_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            brew = self._touch_exec(Path(tmp) / "opt" / "homebrew" / "bin" / "llama-server")

            def fake_well_known(platform_name=None):
                return [(brew, "homebrew")]

            with mock.patch.object(bridge, "_well_known_llama_paths", side_effect=fake_well_known):
                info = bridge.resolve_llama_bin(
                    settings_bin="",
                    env_bin="",
                    which=lambda n: None,
                    platform_name="darwin",
                    machine="arm64",
                    probe_version=False,
                    skip_scan=True,
                )
            self.assertEqual(info["validation"], "ok")
            self.assertEqual(info["source"], "homebrew")
            self.assertEqual(info["path"], brew)
            self.assertEqual(info["architecture"], "arm64")

    def test_intel_homebrew_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_as = str(Path(tmp) / "opt" / "homebrew" / "bin" / "llama-server")
            brew = self._touch_exec(Path(tmp) / "usr" / "local" / "bin" / "llama-server")

            # Apple Silicon prefix absent → fall through to Intel Homebrew prefix.
            real_paths = [
                (missing_as, "homebrew"),
                (brew, "homebrew"),
            ]
            with mock.patch.object(bridge, "_well_known_llama_paths", return_value=real_paths):
                info = bridge.resolve_llama_bin(
                    settings_bin="",
                    env_bin="",
                    which=lambda n: None,
                    platform_name="darwin",
                    machine="x86_64",
                    probe_version=False,
                    skip_scan=True,
                )
            self.assertEqual(info["validation"], "ok")
            self.assertEqual(info["source"], "homebrew")
            self.assertEqual(info["path"], brew)

    def test_windows_exe_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "llama-server.exe"
            exe.write_bytes(b"MZ")
            # Host may be POSIX; make the stub executable so validation passes.
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            exe_path = str(exe.resolve())

            def fake_which(name):
                if name == "llama-server.exe":
                    return exe_path
                return None

            with mock.patch.object(bridge, "_well_known_llama_paths", return_value=[]):
                info = bridge.resolve_llama_bin(
                    settings_bin="",
                    env_bin="",
                    which=fake_which,
                    platform_name="win32",
                    machine="AMD64",
                    probe_version=False,
                    skip_scan=True,
                )
            self.assertEqual(info["basename"], "llama-server.exe")
            self.assertEqual(info["detected_platform"], "windows")
            self.assertEqual(info["validation"], "ok")
            self.assertEqual(info["source"], "path")
            self.assertEqual(info["path"], exe_path)

    def test_missing_executable(self):
        with mock.patch.object(bridge, "_well_known_llama_paths", return_value=[]):
            info = bridge.resolve_llama_bin(
                settings_bin="",
                env_bin="",
                which=lambda n: None,
                platform_name="darwin",
                machine="arm64",
                probe_version=False,
                skip_scan=True,
            )
        self.assertEqual(info["validation"], "missing")
        self.assertEqual(info["path"], "")
        self.assertEqual(info["basename"], "llama-server")
        self.assertIn("brew install", (info["hint"] or "").lower())

        with mock.patch.object(bridge, "resolve_llama_bin", return_value=info):
            self.assertEqual(bridge.find_llama_bin(), "")

    def test_non_executable_file(self):
        if sys.platform == "win32":
            self.skipTest("POSIX executable-bit semantics")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llama-server"
            path.write_text("not exec", encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # no +x
            v = bridge.validate_llama_executable(str(path))
            self.assertFalse(v["ok"])
            self.assertEqual(v["validation"], "not_executable")

            info = bridge.resolve_llama_bin(
                settings_bin=str(path),
                env_bin="",
                which=lambda n: None,
                platform_name="darwin",
                probe_version=False,
                skip_scan=True,
            )
            self.assertEqual(info["source"], "settings")
            self.assertEqual(info["validation"], "not_executable")

    def test_version_command_timeout(self):
        def slow_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0] if args else "x", timeout=2)

        with tempfile.TemporaryDirectory() as tmp:
            bin_path = self._touch_exec(Path(tmp) / "llama-server")
            ver = bridge.probe_llama_version(bin_path, timeout=0.5, run=slow_run)
            self.assertIsNone(ver)

            info = bridge.resolve_llama_bin(
                settings_bin=bin_path,
                env_bin="",
                which=lambda n: None,
                platform_name="darwin",
                machine="arm64",
                probe_version=True,
                version_timeout=0.5,
                version_runner=slow_run,
                skip_scan=True,
            )
            # Path still valid; version simply unavailable — startup must not hang/fail.
            self.assertEqual(info["validation"], "ok")
            self.assertEqual(info["path"], bin_path)
            self.assertIsNone(info["version"])

    def test_paths_containing_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_path = self._touch_exec(
                Path(tmp) / "My Models" / "llama tools" / "llama-server"
            )

            def fake_run(cmd, **kwargs):
                self.assertEqual(cmd[0], bin_path)
                self.assertEqual(cmd[1], "--version")
                return subprocess.CompletedProcess(cmd, 0, stdout="version: test (spaces)\n", stderr="")

            info = bridge.resolve_llama_bin(
                settings_bin=bin_path,
                env_bin="",
                which=lambda n: None,
                platform_name="darwin",
                machine="arm64",
                probe_version=True,
                version_runner=fake_run,
                skip_scan=True,
            )
            self.assertEqual(info["validation"], "ok")
            self.assertEqual(info["path"], bin_path)
            self.assertIn("My Models", info["path"])
            self.assertIn("llama tools", info["path"])
            self.assertIn("version: test", info["version"] or "")

    def test_basename_platform_neutral(self):
        self.assertEqual(bridge.llama_server_basename("darwin"), "llama-server")
        self.assertEqual(bridge.llama_server_basename("linux"), "llama-server")
        self.assertEqual(bridge.llama_server_basename("win32"), "llama-server.exe")

    def test_well_known_includes_both_homebrew_prefixes(self):
        paths = [p for p, _ in bridge._well_known_llama_paths("darwin")]
        self.assertIn("/opt/homebrew/bin/llama-server", paths)
        self.assertIn("/usr/local/bin/llama-server", paths)
        self.assertTrue(all(not p.endswith(".exe") for p in paths if "homebrew" in p or "usr/local" in p))

    def test_error_message_not_exe_on_macos(self):
        info = bridge._empty_llama_resolution(
            platform_name="darwin",
            machine="arm64",
            validation="missing",
            error="llama-server not found",
        )
        msg = bridge._llama_bin_error_message(info)
        self.assertNotIn("llama-server.exe", msg)
        self.assertIn("llama-server", msg)


if __name__ == "__main__":
    unittest.main()

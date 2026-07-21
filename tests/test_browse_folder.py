"""Tests for the native folder picker used by /api/browse-folder.

Run: python -m unittest tests.test_browse_folder -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

# Ensure repo root is importable when running from any cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge


class PickFolderHelpersTest(unittest.TestCase):
    def tearDown(self):
        bridge._pick_folder_impl = None
        os.environ.pop("ACCURETTA_NO_GUI", None)

    def test_successful_folder_selection(self):
        bridge._pick_folder_impl = lambda title: bridge._picker_ok("/Users/me/Models")
        result = bridge.pick_folder("Pick models folder")
        self.assertEqual(result["path"], "/Users/me/Models")
        self.assertFalse(result["cancelled"])
        self.assertIsNone(result["error"])
        self.assertIsNone(result["code"])

    def test_user_cancellation(self):
        bridge._pick_folder_impl = lambda title: bridge._picker_cancel()
        result = bridge.pick_folder("Pick a folder")
        self.assertEqual(result["path"], "")
        self.assertTrue(result["cancelled"])
        self.assertIsNone(result["error"])
        self.assertIsNone(result["code"])

    def test_dialog_unavailable(self):
        bridge._pick_folder_impl = lambda title: bridge._picker_error(
            "unavailable", "Folder picker unavailable."
        )
        result = bridge.pick_folder()
        self.assertEqual(result["path"], "")
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["code"], "unavailable")
        self.assertIn("Paste the folder path", result["message"])
        self.assertNotIn("Traceback", result["message"] or "")
        self.assertNotIn("Traceback", result["error"] or "")

    def test_native_dialog_exception_does_not_propagate(self):
        def boom(_title):
            raise RuntimeError("simulated native dialog failure\nTraceback (most recent call last):")

        bridge._pick_folder_impl = boom
        result = bridge.pick_folder()
        self.assertEqual(result["path"], "")
        self.assertEqual(result["code"], "picker_failed")
        self.assertIn("Paste the folder path", result["message"])
        # Never leak Python tracebacks to the API consumer.
        self.assertNotIn("Traceback", result["error"] or "")
        self.assertNotIn("Traceback", result["message"] or "")

    def test_headless_environment(self):
        os.environ["ACCURETTA_NO_GUI"] = "1"
        result = bridge.pick_folder("Pick a folder")
        self.assertEqual(result["code"], "no_gui")
        self.assertEqual(result["path"], "")
        self.assertIn("Paste the folder path", result["message"])

    def test_macos_path_containing_spaces(self):
        fake = subprocess.CompletedProcess(
            args=["osascript"],
            returncode=0,
            stdout="/Users/me/My Models/GGUF Folder/\n",
            stderr="",
        )
        with mock.patch.object(bridge.sys, "platform", "darwin"), \
             mock.patch.object(bridge.subprocess, "run", return_value=fake) as run:
            result = bridge.pick_folder("Pick models folder")
        self.assertEqual(result["path"], "/Users/me/My Models/GGUF Folder")
        self.assertFalse(result["cancelled"])
        self.assertIsNone(result["error"])
        # Prompt is passed through osascript -e
        args = run.call_args[0][0]
        self.assertEqual(args[0], "osascript")
        self.assertIn("choose folder", args[2])

    def test_macos_osascript_user_cancel(self):
        fake = subprocess.CompletedProcess(
            args=["osascript"],
            returncode=1,
            stdout="",
            stderr="User canceled.",
        )
        with mock.patch.object(bridge.sys, "platform", "darwin"), \
             mock.patch.object(bridge.subprocess, "run", return_value=fake):
            result = bridge.pick_folder()
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["path"], "")
        self.assertIsNone(result["code"])

    def test_macos_never_calls_tkinter(self):
        fake = subprocess.CompletedProcess(
            args=["osascript"], returncode=0, stdout="/tmp/ok\n", stderr=""
        )
        with mock.patch.object(bridge.sys, "platform", "darwin"), \
             mock.patch.object(bridge.subprocess, "run", return_value=fake), \
             mock.patch.object(bridge, "_pick_folder_tk") as tk_picker:
            result = bridge.pick_folder()
        tk_picker.assert_not_called()
        self.assertEqual(result["path"], "/tmp/ok")

    def test_windows_behavior_uses_tkinter(self):
        with mock.patch.object(bridge.sys, "platform", "win32"), \
             mock.patch.object(
                 bridge, "_pick_folder_tk",
                 return_value=bridge._picker_ok(r"C:\Users\me\MODELS"),
             ) as tk_picker, \
             mock.patch.object(bridge, "_pick_folder_osascript") as osa:
            result = bridge.pick_folder("Pick models folder")
        tk_picker.assert_called_once_with("Pick models folder")
        osa.assert_not_called()
        self.assertEqual(result["path"], r"C:\Users\me\MODELS")

    def test_linux_behavior_uses_tkinter(self):
        with mock.patch.object(bridge.sys, "platform", "linux"), \
             mock.patch.object(
                 bridge, "_pick_folder_tk",
                 return_value=bridge._picker_ok("/home/me/models"),
             ) as tk_picker, \
             mock.patch.object(bridge, "_pick_folder_osascript") as osa:
            result = bridge.pick_folder()
        tk_picker.assert_called_once()
        osa.assert_not_called()
        self.assertEqual(result["path"], "/home/me/models")

    def test_tk_cancel_empty_path(self):
        """Preserve prior Windows/Linux cancel semantics (empty askdirectory)."""
        class FakeRoot:
            def withdraw(self): pass
            def attributes(self, *a, **k): pass
            def destroy(self): pass

        class FakeTkModule:
            def Tk(self):
                return FakeRoot()

        class FakeFileDialog:
            @staticmethod
            def askdirectory(title=""):
                return ""

        fake_tk = FakeTkModule()
        # filedialog is imported as `from tkinter import filedialog`.
        fake_tk.filedialog = FakeFileDialog()
        with mock.patch.dict(sys.modules, {
            "tkinter": fake_tk,
            "tkinter.filedialog": FakeFileDialog(),
        }):
            result = bridge._pick_folder_tk("Pick a folder")
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["path"], "")

    def test_tk_exception_becomes_structured_error(self):
        class FakeFileDialog:
            @staticmethod
            def askdirectory(title=""):
                return "/unused"

        class BoomTk:
            filedialog = FakeFileDialog()

            def Tk(self):
                raise RuntimeError("display missing\nTraceback (most recent call last):")

        with mock.patch.dict(sys.modules, {
            "tkinter": BoomTk(),
            "tkinter.filedialog": FakeFileDialog(),
        }):
            result = bridge._pick_folder_tk("Pick a folder")
        self.assertEqual(result["code"], "picker_failed")
        self.assertNotIn("Traceback", result["error"] or "")
        self.assertIn("Paste the folder path", result["message"])


class BrowseFolderApiSafeTest(unittest.TestCase):
    """API handler must return structured JSON and never abort on picker failure."""

    def tearDown(self):
        bridge._pick_folder_impl = None
        os.environ.pop("ACCURETTA_NO_GUI", None)

    def _post_browse(self, body: dict):
        sent = {}

        class FakeHandler(bridge.Handler):
            def __init__(self):
                # Skip BaseHTTPRequestHandler.__init__ (needs a real request).
                # Loopback peer — remote Tailscale peers are rejected separately.
                self.client_address = ("127.0.0.1", 0)

            def _read_json(self):
                return body

            def _send_json(self, status, obj):
                sent["status"] = status
                sent["body"] = obj
                return None

        h = FakeHandler()
        h._handle_api_post("/api/browse-folder", None)
        return sent

    def test_api_success_without_process_exit(self):
        bridge._pick_folder_impl = lambda title: bridge._picker_ok("/tmp/models")
        sent = self._post_browse({"title": "Pick models folder"})
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["path"], "/tmp/models")

    def test_api_safe_error_without_terminating(self):
        def boom(_title):
            raise RuntimeError("boom\nTraceback (most recent call last):\n  File \"x\"")

        bridge._pick_folder_impl = boom
        # Must not raise out of the handler — process stays alive.
        sent = self._post_browse({})
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["path"], "")
        self.assertEqual(sent["body"]["code"], "picker_failed")
        self.assertIn("Paste the folder path", sent["body"]["message"])
        payload = json.dumps(sent["body"])
        self.assertNotIn("Traceback", payload)

    def test_api_headless_structured_error(self):
        os.environ["ACCURETTA_NO_GUI"] = "true"
        sent = self._post_browse({"title": "Pick a folder"})
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["code"], "no_gui")
        self.assertIn("Paste the folder path", sent["body"]["message"])


if __name__ == "__main__":
    unittest.main()

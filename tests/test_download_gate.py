"""Windows-only llama.cpp zip download gate.

Run: python -m unittest tests.test_download_gate -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge


class WindowsZipDownloadGateTest(unittest.TestCase):
    def test_allowed_only_on_windows(self):
        self.assertTrue(bridge.windows_llama_zip_download_allowed("win32"))
        self.assertTrue(bridge.windows_llama_zip_download_allowed("windows"))
        self.assertFalse(bridge.windows_llama_zip_download_allowed("darwin"))
        self.assertFalse(bridge.windows_llama_zip_download_allowed("macos"))
        self.assertFalse(bridge.windows_llama_zip_download_allowed("linux"))

    def test_download_helper_refuses_on_macos(self):
        with mock.patch.object(bridge, "windows_llama_zip_download_allowed", return_value=False):
            with bridge.DOWNLOAD_LOCK:
                bridge.DOWNLOAD_STATE.update({"status": "idle", "error_msg": ""})
            bridge.download_and_extract_llama("CPU")
            with bridge.DOWNLOAD_LOCK:
                self.assertEqual(bridge.DOWNLOAD_STATE.get("status"), "failed")
                self.assertIn("not available", bridge.DOWNLOAD_STATE.get("error_msg", "").lower())

    def test_download_helper_does_not_start_urlopen_when_refused(self):
        with mock.patch.object(bridge, "windows_llama_zip_download_allowed", return_value=False):
            with mock.patch("urllib.request.urlopen") as urlopen:
                bridge.download_and_extract_llama("CUDA")
                urlopen.assert_not_called()

    def test_post_handler_source_checks_gate(self):
        import inspect
        src = inspect.getsource(bridge.Handler._handle_api_post)
        self.assertIn("windows_llama_zip_download_allowed", src)
        self.assertIn("/api/setup/download-llama", src)


class GitignoreModelsTest(unittest.TestCase):
    def test_gitignore_blocks_models_and_gguf(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("models/", text)
        self.assertIn("*.gguf", text)


if __name__ == "__main__":
    unittest.main()

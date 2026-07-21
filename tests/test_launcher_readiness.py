"""Launcher readiness — Accuretta identity vs bare TCP."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from launcher_readiness import (
    ACCURETTA_APP_ID,
    is_accuretta_health_payload,
    port_accepts_tcp,
    probe_accuretta,
    wait_for_accuretta,
)


class _FakeHandler(BaseHTTPRequestHandler):
    payload = {"ok": True, "app": ACCURETTA_APP_ID}

    def do_GET(self):
        if self.path.split("?")[0] != "/api/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class LauncherReadinessTest(unittest.TestCase):
    def test_payload_requires_app_marker(self):
        self.assertTrue(is_accuretta_health_payload({"ok": True, "app": "accuretta"}))
        self.assertFalse(is_accuretta_health_payload({"ok": True}))
        self.assertFalse(is_accuretta_health_payload({"ok": True, "app": "other"}))
        self.assertFalse(is_accuretta_health_payload({"ok": False, "app": "accuretta"}))
        self.assertFalse(is_accuretta_health_payload("nope"))

    def test_probe_rejects_tcp_only_listener(self):
        """Open TCP without Accuretta health must not count as ready."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)

        def _accept():
            try:
                conn, _ = srv.accept()
                conn.close()
            except Exception:
                pass

        th = threading.Thread(target=_accept, daemon=True)
        th.start()
        try:
            self.assertTrue(port_accepts_tcp("127.0.0.1", port))
            self.assertFalse(probe_accuretta("127.0.0.1", port, timeout=0.5))
        finally:
            srv.close()
            th.join(timeout=1)

    def test_probe_accepts_accuretta_health(self):
        httpd = HTTPServer(("127.0.0.1", 0), _FakeHandler)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            self.assertTrue(probe_accuretta("127.0.0.1", port, timeout=1.0))
            self.assertTrue(wait_for_accuretta("127.0.0.1", port, timeout=2.0, poll_s=0.05))
        finally:
            httpd.shutdown()
            th.join(timeout=2)

    def test_probe_rejects_foreign_health_json(self):
        class Foreign(_FakeHandler):
            payload = {"ok": True, "status": "up"}

        httpd = HTTPServer(("127.0.0.1", 0), Foreign)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            self.assertTrue(port_accepts_tcp("127.0.0.1", port))
            self.assertFalse(probe_accuretta("127.0.0.1", port, timeout=1.0))
        finally:
            httpd.shutdown()
            th.join(timeout=2)

    def test_bridge_health_includes_app_marker(self):
        # Source contract — avoid importing the full bridge HTTP stack here.
        text = Path(__file__).resolve().parents[1].joinpath("bridge.py").read_text(encoding="utf-8")
        self.assertIn('"app": "accuretta"', text)
        self.assertIn("/api/health", text)

    def test_desktop_boot_uses_probe_not_tcp_alone(self):
        text = Path(__file__).resolve().parents[1].joinpath("accuretta_app.py").read_text(encoding="utf-8")
        self.assertIn("probe_accuretta", text)
        self.assertIn("_PORT_BUSY_HTML", text)
        self.assertIn("wait_for_accuretta", text)


class CodexShutdownScopeTest(unittest.TestCase):
    def test_terminate_targets_owned_pid_only(self):
        src = Path(__file__).resolve().parents[1].joinpath("codex/process.py").read_text(encoding="utf-8")
        self.assertIn("_owned_pid", src)
        self.assertIn("os.killpg", src)
        self.assertIn("shell=False", src)
        # Ensure Popen is never invoked with shell enabled (ignore docstring mentions).
        self.assertRegex(src, r"Popen\([^)]*shell=False")
        self.assertNotRegex(src, r"Popen\([^)]*shell=True")
        self.assertNotIn('["pkill"', src)
        self.assertNotIn("['pkill'", src)


if __name__ == "__main__":
    unittest.main()

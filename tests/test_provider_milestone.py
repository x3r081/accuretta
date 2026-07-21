"""Milestone finalization tests — diagnostics safety and shutdown."""

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.device_flow import (
    DeviceFlowManager,
    clear_device_config_factories,
    get_device_flow_manager,
    register_device_config_factory,
    reset_device_flow_manager,
)
from auth.device_models import DeviceAuthorizationConfig
from auth.file_store import FileAuthStore
from auth.oauth_client import _post_form
from auth.store import AuthStoreInfo
from providers.management import (
    build_provider_diagnostics,
    list_provider_statuses,
    reset_auth_store_info,
    shutdown_provider_background,
)
from providers.registry import reset_default_registry
from providers.status import assert_safe_provider_payload
from tests.fake_oauth_server import FakeOAuthServer


class ProviderDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        from providers import management as mgmt
        store = FileAuthStore(path=Path(self.tmp.name) / "c.json")
        mgmt._auth_info = AuthStoreInfo(
            store=store, backend="file",
            secure_cloud_auth_available=False, detail="test file backend",
        )
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("ACCURETTA_GITHUB_CLIENT_ID", None)

    def tearDown(self):
        self._env.stop()
        reset_auth_store_info()
        reset_default_registry()
        self.tmp.cleanup()

    def test_diagnostics_safe_and_present(self):
        body = list_provider_statuses({"provider_id": "local_llama"})
        assert_safe_provider_payload(body)
        diag = body["diagnostics"]
        assert_safe_provider_payload(diag)
        self.assertTrue(diag["localProviderAvailable"])
        self.assertFalse(diag["githubConfigured"])
        self.assertIsNotNone(diag.get("githubDisabledReason"))
        self.assertEqual(diag["authBackend"], "file")
        blob = json.dumps(body)
        for needle in ("access_token", "refresh_token", "device_code", "Bearer ", "sk-"):
            self.assertNotIn(needle, blob)

    def test_build_diagnostics_standalone(self):
        d = build_provider_diagnostics({})
        assert_safe_provider_payload(d)
        self.assertIn("openaiCredentialStored", d)


class ProviderShutdownTest(unittest.TestCase):
    def setUp(self):
        reset_device_flow_manager()
        clear_device_config_factories()
        self.server = FakeOAuthServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FileAuthStore(path=Path(self.tmp.name) / "c.json")

        def factory():
            return DeviceAuthorizationConfig(
                provider_id="fake_device",
                client_id=self.server.client_id,
                device_authorization_endpoint=self.server.device_url,
                token_endpoint=self.server.token_url,
                scopes=("read:user",),
                min_poll_interval_seconds=1,
                max_poll_interval_seconds=5,
            )

        register_device_config_factory("fake_device", factory)

    def tearDown(self):
        shutdown_provider_background()
        clear_device_config_factories()
        self.tmp.cleanup()

    def test_shutdown_cancels_pending_poller(self):
        def sleep_fn(_s: float) -> bool:
            time.sleep(0.02)
            return True

        mgr = DeviceFlowManager(
            post_form=lambda u, d: _post_form(u, d, return_error_payload=True, timeout=5.0),
            sleep=sleep_fn,
        )
        import auth.device_flow as df
        df._MANAGER = mgr
        mgr.start(
            DeviceAuthorizationConfig(
                provider_id="fake_device",
                client_id=self.server.client_id,
                device_authorization_endpoint=self.server.device_url,
                token_endpoint=self.server.token_url,
                scopes=("read:user",),
                min_poll_interval_seconds=1,
                max_poll_interval_seconds=5,
            ),
            store=self.store,
        )
        self.assertEqual(mgr.status("fake_device")["status"], "pending")
        shutdown_provider_background()
        # Singleton reset — new manager has idle status; prior poller cancelled.
        fresh = get_device_flow_manager()
        self.assertEqual(fresh.status("fake_device")["status"], "idle")
        self.assertIsNone(self.store.load("fake_device"))


class ProviderSmokeDocTest(unittest.TestCase):
    def test_smoke_doc_exists(self):
        root = Path(__file__).resolve().parent.parent
        doc = (root / "docs" / "provider-smoke-test.md").read_text(encoding="utf-8")
        self.assertIn("OpenAI API-key smoke test", doc)
        self.assertIn("GitHub (expected unconfigured behavior)", doc)
        self.assertIn("diagnostics", doc)
        js = (root / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("deviceCode", js)
        self.assertIn("github-auth-section", (root / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

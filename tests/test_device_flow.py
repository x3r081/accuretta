"""Generic OAuth device-authorization tests (fake server only — no network)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge
from auth.device_flow import (
    DeviceFlowManager,
    clear_device_config_factories,
    get_device_flow_manager,
    register_device_config_factory,
    reset_device_flow_manager,
    unregister_device_config_factory,
)
from auth.device_models import DeviceAuthorizationConfig, DeviceFlowStatus
from auth.file_store import FileAuthStore
from auth.models import StoredCredential
from auth.oauth_client import _post_form, request_device_authorization
from auth.store import AuthStoreInfo
from providers.base import ApiMode, AuthType, ProviderCapabilities, ProviderDefinition
from providers.errors import ProviderUnavailable
from providers.management import (
    cancel_device_authorization,
    device_authorization_status,
    reset_auth_store_info,
    start_device_authorization,
)
from providers.registry import get_default_registry, reset_default_registry
from providers.status import assert_safe_provider_payload
from tests.fake_oauth_server import FakeOAuthServer

TEST_PROVIDER_ID = "fake_device"


def _config(server: FakeOAuthServer) -> DeviceAuthorizationConfig:
    return DeviceAuthorizationConfig(
        provider_id=TEST_PROVIDER_ID,
        client_id=server.client_id,
        device_authorization_endpoint=server.device_url,
        token_endpoint=server.token_url,
        scopes=("read:user",),
        min_poll_interval_seconds=1,
        max_poll_interval_seconds=5,
    )


def _wait_status(manager: DeviceFlowManager, provider_id: str, want: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = manager.status(provider_id)
        if last.get("status") == want:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {want}; last={last}")


class DeviceFlowTestCase(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        reset_device_flow_manager()
        clear_device_config_factories()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FileAuthStore(path=Path(self.tmp.name) / "creds.json")
        from providers import management as mgmt
        mgmt._auth_info = AuthStoreInfo(
            store=self.store, backend="file",
            secure_cloud_auth_available=True, detail="test",
        )
        self.server = FakeOAuthServer()
        self.server.start()
        self.addCleanup(self.server.stop)

        def factory():
            return _config(self.server)

        register_device_config_factory(TEST_PROVIDER_ID, factory)
        reg = get_default_registry()
        reg.register(
            ProviderDefinition(
                id=TEST_PROVIDER_ID,
                display_name="Fake Device",
                api_mode=ApiMode.OPENAI_CHAT,
                auth_type=AuthType.OAUTH_DEVICE,
                capabilities=ProviderCapabilities(
                    streaming=False,
                    tools=False,
                    vision=False,
                    cancellation=True,
                    model_listing=False,
                    account_authentication=True,
                    device_authorization=True,
                    inference=False,
                ),
                supports_model_listing=False,
                supports_inference=False,
                experimental=True,
                enabled=True,
            ),
            factory=None,
        )
        self._old_settings = bridge.SETTINGS_FILE
        bridge.SETTINGS_FILE = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        bridge.SETTINGS_FILE = self._old_settings
        reset_device_flow_manager()
        clear_device_config_factories()
        reset_auth_store_info()
        reset_default_registry()
        self.tmp.cleanup()

    def _manager(self) -> DeviceFlowManager:
        def sleep_fn(_seconds: float) -> bool:
            time.sleep(0.02)
            return True

        # Use real HTTP against fake server with return_error_payload.
        def post(url, data):
            return _post_form(url, data, return_error_payload=True, timeout=5.0)

        mgr = DeviceFlowManager(post_form=post, sleep=sleep_fn)
        return mgr

    def test_successful_device_flow(self):
        mgr = self._manager()
        start = mgr.start(_config(self.server), store=self.store)
        assert_safe_provider_payload(start)
        self.assertEqual(start["status"], "pending")
        self.assertEqual(start["userCode"], "ABCD-EFGH")
        self.assertIn("verificationUri", start)
        self.assertNotIn("deviceCode", start)
        self.assertNotIn("device_code", start)
        self.assertIsNone(self.store.load(TEST_PROVIDER_ID))

        code = self.server.latest_device_code()
        self.assertIsNotNone(code)
        self.server.approve_device(code)
        _wait_status(mgr, TEST_PROVIDER_ID, "authorized")
        cred = self.store.load(TEST_PROVIDER_ID)
        self.assertIsNotNone(cred)
        self.assertTrue(cred.access_token.startswith("access-device-"))
        self.assertNotIn(code, json.dumps(mgr.status(TEST_PROVIDER_ID)))

    def test_authorization_pending_then_success(self):
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        st = mgr.status(TEST_PROVIDER_ID)
        self.assertEqual(st["status"], "pending")
        time.sleep(0.1)
        self.assertEqual(mgr.status(TEST_PROVIDER_ID)["status"], "pending")
        self.server.approve_device(self.server.latest_device_code())
        _wait_status(mgr, TEST_PROVIDER_ID, "authorized")

    def test_slow_down(self):
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        code = self.server.latest_device_code()
        self.server.mark_device_slow_down(code)
        time.sleep(0.15)
        self.server.approve_device(code)
        _wait_status(mgr, TEST_PROVIDER_ID, "authorized")

    def test_denial(self):
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        self.server.deny_device(self.server.latest_device_code())
        _wait_status(mgr, TEST_PROVIDER_ID, "denied")
        self.assertIsNone(self.store.load(TEST_PROVIDER_ID))

    def test_expiration(self):
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        self.server.expire_device(self.server.latest_device_code())
        _wait_status(mgr, TEST_PROVIDER_ID, "expired")
        self.assertIsNone(self.store.load(TEST_PROVIDER_ID))

    def test_cancellation(self):
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        out = mgr.cancel(TEST_PROVIDER_ID)
        self.assertEqual(out["status"], "cancelled")
        time.sleep(0.2)
        self.assertIsNone(self.store.load(TEST_PROVIDER_ID))
        self.assertEqual(mgr.status(TEST_PROVIDER_ID)["status"], "cancelled")

    def test_transient_network_and_5xx(self):
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        code = self.server.latest_device_code()
        self.server.device_poll_behavior[code] = [
            {"error": "server_error", "http_status": 503},
            {"error": "server_error", "http_status": 429},
        ]
        time.sleep(0.2)
        self.server.approve_device(code)
        _wait_status(mgr, TEST_PROVIDER_ID, "authorized", timeout=8.0)

    def test_invalid_client_does_not_delete_other_creds(self):
        other = StoredCredential(
            provider_id="openai",
            access_token="sk-keep-me-secret-token-xxxx",
            token_type="api_key",
            metadata={"credential_type": "api_key"},
        )
        self.store.save("openai", other)
        self.server.force_device_error = "invalid_client"
        mgr = self._manager()
        with self.assertRaises(Exception):
            mgr.start(_config(self.server), store=self.store)
        self.assertIsNotNone(self.store.load("openai"))
        self.assertEqual(self.store.load("openai").access_token, other.access_token)

    def test_malformed_device_response(self):
        def bad_request(config, **kwargs):
            raise RuntimeError("boom")

        mgr = DeviceFlowManager(
            request_device=bad_request,
            sleep=lambda s: True,
            post_form=lambda u, d: {},
        )
        with self.assertRaises(Exception):
            mgr.start(_config(self.server), store=self.store)

    def test_stale_session_protection(self):
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        old_code = self.server.latest_device_code()
        # Supersede with a new flow.
        mgr.start(_config(self.server), store=self.store)
        new_code = self.server.latest_device_code()
        self.assertNotEqual(old_code, new_code)
        # Approving the stale code must not save credentials for the new session.
        self.server.approve_device(old_code)
        time.sleep(0.25)
        self.assertIsNone(self.store.load(TEST_PROVIDER_ID))
        self.server.approve_device(new_code)
        _wait_status(mgr, TEST_PROVIDER_ID, "authorized")
        self.assertIsNotNone(self.store.load(TEST_PROVIDER_ID))

    def test_concurrent_start_cancels_prior(self):
        mgr = self._manager()
        first = mgr.start(_config(self.server), store=self.store)
        second = mgr.start(_config(self.server), store=self.store)
        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(mgr.status(TEST_PROVIDER_ID)["userCode"], second["userCode"])

    def test_secret_redaction_and_frontend_response(self):
        mgr = self._manager()
        start = mgr.start(_config(self.server), store=self.store)
        blob = json.dumps(start)
        code = self.server.latest_device_code()
        self.assertNotIn(code, blob)
        self.assertNotIn("access_token", blob)
        self.assertNotIn("device_code", blob)
        assert_safe_provider_payload(start)
        rep = repr(self.store)  # store itself ok
        _ = rep
        # Result repr hides tokens
        from auth.device_models import DeviceAuthorizationResult
        r = DeviceAuthorizationResult(access_token="secret-token-value", refresh_token="r")
        self.assertNotIn("secret-token-value", repr(r))

    def test_credentials_saved_only_after_success(self):
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        self.assertIsNone(self.store.load(TEST_PROVIDER_ID))
        mgr.cancel(TEST_PROVIDER_ID)
        self.assertIsNone(self.store.load(TEST_PROVIDER_ID))

    def test_no_credential_overwrite_after_cancellation(self):
        existing = StoredCredential(
            provider_id=TEST_PROVIDER_ID,
            access_token="existing-access-token-value",
            refresh_token="existing-refresh",
            metadata={"credential_type": "oauth_device", "account_label": "keep"},
        )
        self.store.save(TEST_PROVIDER_ID, existing)
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        mgr.cancel(TEST_PROVIDER_ID)
        time.sleep(0.15)
        cred = self.store.load(TEST_PROVIDER_ID)
        self.assertIsNotNone(cred)
        self.assertEqual(cred.access_token, "existing-access-token-value")

    def test_api_start_status_cancel(self):
        # Wire singleton manager used by management helpers.
        reset_device_flow_manager()

        def sleep_fn(_s: float) -> bool:
            time.sleep(0.02)
            return True

        mgr = DeviceFlowManager(
            post_form=lambda u, d: _post_form(u, d, return_error_payload=True, timeout=5.0),
            sleep=sleep_fn,
        )
        import auth.device_flow as df
        df._MANAGER = mgr

        start = start_device_authorization(TEST_PROVIDER_ID, {})
        assert_safe_provider_payload(start)
        self.assertNotIn("deviceCode", start)
        self.assertEqual(start["userCode"], "ABCD-EFGH")

        st = device_authorization_status(TEST_PROVIDER_ID, {})
        self.assertEqual(st["status"], "pending")

        code = self.server.latest_device_code()
        self.server.approve_device(code)
        deadline = time.time() + 5
        while time.time() < deadline:
            st = device_authorization_status(TEST_PROVIDER_ID, {})
            if st.get("status") == "authorized":
                break
            time.sleep(0.05)
        self.assertEqual(st["status"], "authorized")
        assert_safe_provider_payload(st)
        self.assertIsNotNone(self.store.load(TEST_PROVIDER_ID))

    def test_api_cancel(self):
        reset_device_flow_manager()

        def sleep_fn(_s: float) -> bool:
            time.sleep(0.02)
            return True

        mgr = DeviceFlowManager(
            post_form=lambda u, d: _post_form(u, d, return_error_payload=True, timeout=5.0),
            sleep=sleep_fn,
        )
        import auth.device_flow as df
        df._MANAGER = mgr
        start_device_authorization(TEST_PROVIDER_ID, {})
        out = cancel_device_authorization(TEST_PROVIDER_ID, {})
        self.assertEqual(out["status"], "cancelled")
        self.assertIsNone(self.store.load(TEST_PROVIDER_ID))

    def test_bridge_handlers_safe(self):
        reset_device_flow_manager()

        def sleep_fn(_s: float) -> bool:
            time.sleep(0.02)
            return True

        mgr = DeviceFlowManager(
            post_form=lambda u, d: _post_form(u, d, return_error_payload=True, timeout=5.0),
            sleep=sleep_fn,
        )
        import auth.device_flow as df
        df._MANAGER = mgr

        class _H(bridge.Handler):
            def __init__(self):
                self.client_address = ("127.0.0.1", 0)
                self._sent = {}

            def _send_json(self, status, obj):
                self._sent["status"] = status
                self._sent["body"] = obj

        h = _H()
        h._handle_providers_post(f"/api/providers/{TEST_PROVIDER_ID}/device/start", {})
        self.assertEqual(h._sent["status"], 200)
        body = h._sent["body"]
        assert_safe_provider_payload(body)
        self.assertNotIn("deviceCode", body)
        blob = json.dumps(body)
        self.assertNotIn(self.server.latest_device_code(), blob)

        h._handle_providers_get(f"/api/providers/{TEST_PROVIDER_ID}/device/status")
        self.assertEqual(h._sent["status"], 200)
        assert_safe_provider_payload(h._sent["body"])

        h._handle_providers_post(f"/api/providers/{TEST_PROVIDER_ID}/device/cancel", {})
        self.assertEqual(h._sent["body"]["status"], "cancelled")

    def test_openai_connect_unaffected_by_device_cancel(self):
        self.store.save(
            "openai",
            StoredCredential(
                provider_id="openai",
                access_token="sk-openai-unrelated-key-zzzz",
                token_type="api_key",
            ),
        )
        mgr = self._manager()
        mgr.start(_config(self.server), store=self.store)
        mgr.cancel(TEST_PROVIDER_ID)
        self.assertEqual(
            self.store.load("openai").access_token,
            "sk-openai-unrelated-key-zzzz",
        )

    def test_request_device_authorization_direct(self):
        session = request_device_authorization(
            _config(self.server),
            post_form=lambda u, d: _post_form(u, d, timeout=5.0),
        )
        self.assertEqual(session.user_code, "ABCD-EFGH")
        self.assertTrue(session.device_code.startswith("device-"))
        # device_code must not appear in safe payload
        from auth.device_models import safe_device_start_payload
        safe = safe_device_start_payload(session)
        assert_safe_provider_payload(safe)
        self.assertNotIn(session.device_code, json.dumps(safe))


if __name__ == "__main__":
    unittest.main()

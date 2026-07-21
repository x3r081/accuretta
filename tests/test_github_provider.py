"""GitHub account authentication tests (fully mocked — no network)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge
from auth.device_flow import (
    DeviceFlowManager,
    clear_device_config_factories,
    register_device_config_factory,
    reset_device_flow_manager,
)
from auth.device_models import DeviceAuthorizationConfig, DeviceAuthorizationResult
from auth.file_store import FileAuthStore
from auth.models import StoredCredential
from auth.oauth_client import _post_form
from auth.store import AuthStoreInfo
from providers.errors import AuthenticationRequired, ProviderUnavailable
from providers.github_provider import (
    GITHUB_PROVIDER_ID,
    GITHUB_SCOPES,
    build_github_definition,
    ensure_github_registered,
    store_github_device_result,
    validate_github_account,
)
from providers.management import (
    cancel_device_authorization,
    device_authorization_status,
    disconnect_provider,
    get_provider_status,
    list_provider_statuses,
    reset_auth_store_info,
    select_provider,
    start_device_authorization,
)
from providers.openai_provider import FAKE_KEY_MARKER, api_key_credential
from providers.registry import get_default_registry, reset_default_registry
from providers.status import assert_safe_provider_payload
from tests.fake_oauth_server import FakeOAuthServer


class GitHubProviderUnavailableTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        reset_device_flow_manager()
        clear_device_config_factories()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("ACCURETTA_GITHUB_CLIENT_ID", None)

    def tearDown(self):
        self._env.stop()
        reset_device_flow_manager()
        clear_device_config_factories()
        reset_auth_store_info()
        reset_default_registry()

    def test_unavailable_without_client_id(self):
        ensure_github_registered()
        d = build_github_definition()
        self.assertFalse(d.enabled)
        self.assertIn("ACCURETTA_GITHUB_CLIENT_ID", d.disabled_reason or "")
        status = get_provider_status("github", {})
        assert_safe_provider_payload(status)
        self.assertFalse(status["available"])
        self.assertFalse(status["supportsInference"])
        self.assertIn("account_authentication", status["capabilities"])
        self.assertIn("device_authorization", status["capabilities"])
        self.assertNotIn("inference", status["capabilities"])
        self.assertNotIn("streaming", status["capabilities"])
        # No Copilot capability advertised
        blob = json.dumps(status).lower()
        self.assertNotIn("copilot", blob)


class GitHubAuthFlowTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        reset_device_flow_manager()
        clear_device_config_factories()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FileAuthStore(path=Path(self.tmp.name) / "c.json")
        from providers import management as mgmt
        mgmt._auth_info = AuthStoreInfo(
            store=self.store, backend="file",
            secure_cloud_auth_available=True, detail="test",
        )
        self.server = FakeOAuthServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self._env = mock.patch.dict(
            os.environ,
            {"ACCURETTA_GITHUB_CLIENT_ID": self.server.client_id},
            clear=False,
        )
        self._env.start()

        def factory():
            return DeviceAuthorizationConfig(
                provider_id=GITHUB_PROVIDER_ID,
                client_id=self.server.client_id,
                device_authorization_endpoint=self.server.device_url,
                token_endpoint=self.server.token_url,
                scopes=GITHUB_SCOPES,
                min_poll_interval_seconds=1,
                max_poll_interval_seconds=5,
            )

        # Patch before register so ensure_builtin_providers cannot re-point at
        # real GitHub endpoints during tests.
        self._cfg_patch = mock.patch(
            "providers.github_provider.github_device_config",
            factory,
        )
        self._cfg_patch.start()
        ensure_github_registered()
        self._old = bridge.SETTINGS_FILE
        bridge.SETTINGS_FILE = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        bridge.SETTINGS_FILE = self._old
        self._cfg_patch.stop()
        self._env.stop()
        reset_device_flow_manager()
        clear_device_config_factories()
        reset_auth_store_info()
        reset_default_registry()
        self.tmp.cleanup()

    def _install_manager(self):
        def sleep_fn(_s: float) -> bool:
            time.sleep(0.02)
            return True

        mgr = DeviceFlowManager(
            post_form=lambda u, d: _post_form(u, d, return_error_payload=True, timeout=5.0),
            sleep=sleep_fn,
        )
        import auth.device_flow as df
        df._MANAGER = mgr
        return mgr

    def test_safe_start_response(self):
        self._install_manager()
        with mock.patch(
            "providers.github_provider.validate_github_account",
            return_value={"account_label": "octocat", "account_id": "1", "validated_at": time.time()},
        ):
            start = start_device_authorization(GITHUB_PROVIDER_ID, {})
        assert_safe_provider_payload(start)
        self.assertEqual(start["status"], "pending")
        self.assertIn("userCode", start)
        self.assertNotIn("deviceCode", start)
        self.assertNotIn("accessToken", start)
        blob = json.dumps(start)
        self.assertNotIn(self.server.latest_device_code(), blob)

    def test_successful_authentication_stores_label(self):
        self._install_manager()
        with mock.patch(
            "providers.github_provider.validate_github_account",
            return_value={"account_label": "octocat", "account_id": "42", "validated_at": 1.0},
        ):
            start_device_authorization(GITHUB_PROVIDER_ID, {})
            self.server.approve_device(self.server.latest_device_code())
            deadline = time.time() + 5
            while time.time() < deadline:
                st = device_authorization_status(GITHUB_PROVIDER_ID, {})
                if st.get("status") == "authorized":
                    break
                time.sleep(0.05)
            self.assertEqual(st["status"], "authorized")
            assert_safe_provider_payload(st)
            self.assertNotIn("access_token", json.dumps(st))
        cred = self.store.load(GITHUB_PROVIDER_ID)
        self.assertIsNotNone(cred)
        self.assertEqual(cred.metadata.get("account_label"), "octocat")
        self.assertEqual(cred.metadata.get("account_id"), "42")
        status = get_provider_status(GITHUB_PROVIDER_ID, {})
        self.assertEqual(status["accountLabel"], "octocat")
        self.assertTrue(status["authenticated"])
        assert_safe_provider_payload(status)

    def test_cannot_select_for_inference(self):
        with self.assertRaises(ProviderUnavailable) as ctx:
            select_provider(GITHUB_PROVIDER_ID, {}, save=lambda s: None)
        self.assertIn("cannot be selected for chat", str(ctx.exception).lower())

    def test_account_validation_401(self):
        def transport(*_a, **_k):
            raise AuthenticationRequired(
                "GitHub rejected the access token",
                provider_id="github",
            )

        with self.assertRaises(AuthenticationRequired):
            validate_github_account("bad-token", transport=transport)

    def test_account_validation_403_via_store(self):
        result = DeviceAuthorizationResult(access_token="tok", scopes=GITHUB_SCOPES)

        def boom(_tok):
            raise AuthenticationRequired("forbidden", provider_id="github")

        with self.assertRaises(AuthenticationRequired):
            store_github_device_result(self.store, result, validate=boom)
        self.assertIsNone(self.store.load(GITHUB_PROVIDER_ID))

    def test_validation_rate_limit(self):
        def boom(_tok):
            raise ProviderUnavailable("rate", provider_id="github")

        with self.assertRaises(ProviderUnavailable):
            store_github_device_result(
                self.store,
                DeviceAuthorizationResult(access_token="tok"),
                validate=boom,
            )
        self.assertIsNone(self.store.load(GITHUB_PROVIDER_ID))

    def test_transient_outage_on_validation(self):
        def boom(_tok):
            raise ProviderUnavailable("outage", provider_id="github")

        with self.assertRaises(ProviderUnavailable):
            store_github_device_result(
                self.store,
                DeviceAuthorizationResult(access_token="tok"),
                validate=boom,
            )

    def test_disconnect(self):
        self.store.save(
            GITHUB_PROVIDER_ID,
            StoredCredential(
                provider_id=GITHUB_PROVIDER_ID,
                access_token="gh-token-secret",
                metadata={"account_label": "octocat", "credential_type": "oauth_device"},
            ),
        )
        out = disconnect_provider(GITHUB_PROVIDER_ID, {})
        self.assertTrue(out["disconnected"])
        self.assertIsNone(self.store.load(GITHUB_PROVIDER_ID))

    def test_pending_session_cancellation(self):
        self._install_manager()
        with mock.patch(
            "providers.github_provider.validate_github_account",
            return_value={"account_label": "x", "account_id": "1", "validated_at": 1.0},
        ):
            start_device_authorization(GITHUB_PROVIDER_ID, {})
            out = cancel_device_authorization(GITHUB_PROVIDER_ID, {})
        self.assertEqual(out["status"], "cancelled")
        self.assertIsNone(self.store.load(GITHUB_PROVIDER_ID))

    def test_status_never_includes_token(self):
        self.store.save(
            GITHUB_PROVIDER_ID,
            StoredCredential(
                provider_id=GITHUB_PROVIDER_ID,
                access_token="gh-super-secret-token",
                metadata={"account_label": "octocat", "credential_validated": True},
            ),
        )
        status = get_provider_status(GITHUB_PROVIDER_ID, {})
        blob = json.dumps(status)
        self.assertNotIn("gh-super-secret-token", blob)
        assert_safe_provider_payload(status)

    def test_no_copilot_capability(self):
        d = build_github_definition()
        self.assertFalse(d.supports_inference)
        self.assertFalse(d.capabilities.inference)
        self.assertFalse(d.capabilities.streaming)
        self.assertFalse(d.capabilities.model_listing)
        listing = list_provider_statuses({})
        gh = next(p for p in listing["providers"] if p["providerId"] == "github")
        self.assertNotIn("copilot", json.dumps(gh).lower())
        self.assertFalse(gh["supportsInference"])

    def test_unrelated_provider_credentials_remain(self):
        self.store.save("openai", api_key_credential(FAKE_KEY_MARKER, validated=True))
        self._install_manager()
        with mock.patch(
            "providers.github_provider.validate_github_account",
            return_value={"account_label": "x", "account_id": "1", "validated_at": 1.0},
        ):
            start_device_authorization(GITHUB_PROVIDER_ID, {})
            cancel_device_authorization(GITHUB_PROVIDER_ID, {})
        self.assertEqual(self.store.load("openai").access_token, FAKE_KEY_MARKER)

    def test_ui_contract(self):
        html = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn("github-auth-section", html)
        self.assertIn("Connect GitHub", html)
        self.assertIn("Copilot inference is not enabled", html)
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/providers/github/device/start", js)
        self.assertIn("supportsInference === false", js)
        self.assertNotIn("localStorage.setItem(\"github", js)
        self.assertNotIn("deviceCode", js)
        self.assertNotIn("device_code", js)

    def test_scopes_documented_minimal(self):
        self.assertEqual(GITHUB_SCOPES, ("read:user",))


if __name__ == "__main__":
    unittest.main()

"""Provider management API and safe status serialization tests.

Run: python3 -m unittest tests.test_providers_api -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge
from auth.file_store import FileAuthStore
from auth.models import StoredCredential
from providers.management import (
    disconnect_provider,
    get_provider_status,
    list_models_for_provider,
    list_provider_statuses,
    reset_auth_store_info,
    resolve_chat_provider,
    select_provider,
)
from providers.registry import get_default_registry, reset_default_registry
from providers.selection import DEFAULT_PROVIDER_ID, resolve_provider_selection
from providers.status import assert_safe_provider_payload, build_safe_provider_status
from providers.errors import ProviderNotConfigured, ProviderUnavailable
from providers.base import ApiMode, AuthType, ProviderCapabilities, ProviderDefinition


class _FakeHandler(bridge.Handler):
    def __init__(self):
        self.client_address = ("127.0.0.1", 0)
        self._sent = {}

    def _send_json(self, status, obj):
        self._sent["status"] = status
        self._sent["body"] = obj
        return None


class ProviderStatusSafetyTest(unittest.TestCase):
    def test_forbidden_keys_rejected(self):
        with self.assertRaises(ValueError):
            assert_safe_provider_payload({"access_token": "secret"})
        with self.assertRaises(ValueError):
            assert_safe_provider_payload({"nested": {"refresh_token": "x"}})
        with self.assertRaises(ValueError):
            assert_safe_provider_payload({"note": "Bearer abc.def.ghi"})

    def test_build_status_has_no_secrets(self):
        d = ProviderDefinition(
            id="local_llama",
            display_name="Local llama.cpp",
            api_mode=ApiMode.LOCAL_LLAMA,
            auth_type=AuthType.NONE,
            capabilities=ProviderCapabilities(),
        )
        status = build_safe_provider_status(
            d, authenticated=True, available=True, selected=True, is_default=True
        )
        assert_safe_provider_payload(status)
        blob = json.dumps(status).lower()
        for needle in ("access_token", "refresh_token", "client_secret", "code_verifier"):
            self.assertNotIn(needle, blob)
        self.assertNotIn("authorization", blob)


class ProviderSelectionPolicyTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        get_default_registry()

    def tearDown(self):
        reset_default_registry()

    def test_registry_contains_local_and_example(self):
        reg = get_default_registry()
        ids = set(reg.ids())
        self.assertIn("local_llama", ids)
        self.assertIn("example_cloud", ids)
        local = reg.get_definition("local_llama")
        self.assertTrue(local.enabled)
        self.assertFalse(local.experimental)
        demo = reg.get_definition("example_cloud")
        self.assertFalse(demo.enabled)
        self.assertTrue(demo.experimental)

    def test_local_is_default(self):
        sel = resolve_provider_selection({})
        self.assertEqual(sel.provider_id, DEFAULT_PROVIDER_ID)
        sel2 = resolve_provider_selection({"provider_id": ""})
        self.assertEqual(sel2.provider_id, DEFAULT_PROVIDER_ID)

    def test_legacy_settings_resolve_local(self):
        # No provider_id key at all
        sel = resolve_provider_selection({"model": "x"})
        self.assertEqual(sel.provider_id, "local_llama")
        self.assertIsNone(sel.warning)

    def test_unknown_setting_falls_back(self):
        sel = resolve_provider_selection({"provider_id": "nope_cloud"})
        self.assertEqual(sel.provider_id, "local_llama")
        self.assertTrue(sel.fell_back)
        self.assertIn("Unknown provider", sel.warning or "")

    def test_chat_blocks_unavailable(self):
        with self.assertRaises(ProviderUnavailable):
            resolve_chat_provider({"provider_id": "example_cloud"})


class ProviderApiHandlersTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.tmp.name) / "settings.json"
        self.cred_path = Path(self.tmp.name) / "credentials.json"
        self._old_settings = bridge.SETTINGS_FILE
        bridge.SETTINGS_FILE = self.settings_path
        # Force file auth store for tests
        from providers import management as mgmt
        from auth.store import AuthStoreInfo
        store = FileAuthStore(path=self.cred_path)
        mgmt._auth_info = AuthStoreInfo(
            store=store,
            backend="file",
            secure_cloud_auth_available=True,
            detail="test file store",
        )

    def tearDown(self):
        bridge.SETTINGS_FILE = self._old_settings
        reset_auth_store_info()
        reset_default_registry()
        self.tmp.cleanup()

    def _get(self, path: str):
        h = _FakeHandler()
        h._handle_providers_get(path)
        return h._sent

    def _post(self, path: str, body=None):
        h = _FakeHandler()
        h._handle_providers_post(path, body or {})
        return h._sent

    def test_get_providers_safe_fields(self):
        sent = self._get("/api/providers")
        self.assertEqual(sent["status"], 200)
        body = sent["body"]
        assert_safe_provider_payload(body)
        self.assertEqual(body["defaultProviderId"], "local_llama")
        ids = {p["providerId"] for p in body["providers"]}
        self.assertIn("local_llama", ids)
        self.assertIn("example_cloud", ids)
        local = next(p for p in body["providers"] if p["providerId"] == "local_llama")
        self.assertTrue(local["isDefault"])
        self.assertTrue(local["available"])
        self.assertTrue(local["selected"])
        demo = next(p for p in body["providers"] if p["providerId"] == "example_cloud")
        self.assertFalse(demo["available"])
        self.assertTrue(demo["experimental"])
        self.assertIn("registration", (demo["disabledReason"] or "").lower())
        blob = json.dumps(body).lower()
        for needle in ("access_token", "refresh_token", "client_secret", "code_verifier", "bearer "):
            self.assertNotIn(needle, blob)

    def test_local_status(self):
        sent = self._get("/api/providers/local_llama/status")
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["providerId"], "local_llama")
        assert_safe_provider_payload(sent["body"])

    def test_unknown_provider_status(self):
        sent = self._get("/api/providers/does_not_exist/status")
        self.assertEqual(sent["status"], 404)
        self.assertEqual(sent["body"]["error"], "provider_not_configured")

    def test_local_model_listing(self):
        from providers.local_llama import LocalLlamaProvider

        with mock.patch.object(
            LocalLlamaProvider,
            "list_models",
            return_value=[{"path": "/tmp/a.gguf", "name": "a.gguf", "provider_id": "local_llama"}],
        ):
            sent = self._get("/api/providers/local_llama/models")
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["providerId"], "local_llama")
        self.assertEqual(len(sent["body"]["models"]), 1)
        assert_safe_provider_payload(sent["body"])

    def test_unavailable_provider_model_listing(self):
        sent = self._get("/api/providers/example_cloud/models")
        self.assertEqual(sent["status"], 409)
        self.assertEqual(sent["body"]["error"], "provider_unavailable")

    def test_select_local_persists(self):
        bridge.save_json(self.settings_path, {"model": "demo"})
        sent = self._post("/api/providers/local_llama/select")
        self.assertEqual(sent["status"], 200)
        self.assertTrue(sent["body"]["ok"])
        saved = bridge.load_json(self.settings_path, {})
        self.assertEqual(saved.get("provider_id"), "local_llama")

    def test_reject_unavailable_selection(self):
        sent = self._post("/api/providers/example_cloud/select")
        self.assertEqual(sent["status"], 409)
        self.assertEqual(sent["body"]["error"], "provider_unavailable")

    def test_local_disconnect_noop(self):
        sent = self._post("/api/providers/local_llama/disconnect")
        self.assertEqual(sent["status"], 200)
        self.assertFalse(sent["body"]["applicable"])
        self.assertFalse(sent["body"]["disconnected"])

    def test_fake_provider_disconnect_deletes_credentials(self):
        from providers import management as mgmt
        store = mgmt.get_auth_store_info().store
        store.save(
            "example_cloud",
            StoredCredential(
                provider_id="example_cloud",
                access_token="test-access-token-should-not-leak",
                refresh_token="test-refresh-token-should-not-leak",
                metadata={"account_label": "demo-user"},
            ),
        )
        sent = self._post("/api/providers/example_cloud/disconnect")
        self.assertEqual(sent["status"], 200)
        self.assertTrue(sent["body"]["disconnected"])
        self.assertIsNone(store.load("example_cloud"))
        blob = json.dumps(sent["body"]).lower()
        self.assertNotIn("test-access-token", blob)
        self.assertNotIn("test-refresh-token", blob)
        self.assertNotIn("access_token", blob)

    def test_connect_rejected(self):
        sent = self._post("/api/providers/example_cloud/connect")
        self.assertEqual(sent["status"], 409)

    def test_chat_resolves_local(self):
        sel = resolve_chat_provider({"provider_id": "local_llama"})
        self.assertEqual(sel.provider_id, "local_llama")

    def test_chat_gate_on_handler(self):
        h = _FakeHandler()
        with mock.patch.object(bridge, "get_settings", return_value={"provider_id": "example_cloud"}):
            h._handle_chat({"message": "hi", "chat_id": "t1"})
        self.assertEqual(h._sent["status"], 409)
        self.assertEqual(h._sent["body"]["error"], "provider_unavailable")
        blob = json.dumps(h._sent["body"]).lower()
        self.assertNotIn("traceback", blob)
        self.assertNotIn("access_token", blob)

    def test_auth_store_failure_does_not_break_list(self):
        from providers import management as mgmt
        from auth.store import AuthStoreInfo
        from auth.file_store import FileAuthStore

        # Simulate limited cloud auth: still list local safely.
        mgmt._auth_info = AuthStoreInfo(
            store=FileAuthStore(path=self.cred_path),
            backend="file",
            secure_cloud_auth_available=False,
            detail="keyring unavailable",
        )
        body = list_provider_statuses({"provider_id": "local_llama"})
        self.assertEqual(body["selectedProviderId"], "local_llama")
        self.assertFalse(body["secureCloudAuthAvailable"])
        assert_safe_provider_payload(body)

    def test_default_settings_include_provider_id(self):
        self.assertEqual(bridge.DEFAULT_SETTINGS.get("provider_id"), "local_llama")
        # Merged settings without file still default
        with mock.patch.object(bridge, "load_json", return_value={}):
            s = bridge.get_settings()
        self.assertEqual(s.get("provider_id"), "local_llama")


class ProviderUiContractTest(unittest.TestCase):
    """Static checks that the settings UI wires provider controls safely."""

    def test_index_has_provider_section(self):
        html = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="set-provider"', html)
        self.assertIn('id="btn-provider-connect"', html)
        self.assertIn('id="set-openai-key"', html)

    def test_app_js_loads_providers_api(self):
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text(encoding="utf-8")
        self.assertIn('/api/providers', js)
        self.assertIn("loadProviders", js)
        self.assertIn("populateProviderForm", js)
        # Must not invent token fields in provider UI code
        self.assertNotIn("access_token", js)
        self.assertNotIn("refresh_token", js)


if __name__ == "__main__":
    unittest.main()

"""OpenAI API-key provider tests (fully mocked — no network).

Run: python3 -m unittest tests.test_openai_provider -v
"""

from __future__ import annotations

import io
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
from providers.errors import AuthenticationRequired, ProviderUnavailable, RateLimited
from providers.management import (
    connect_provider,
    disconnect_provider,
    get_provider_status,
    list_models_for_provider,
    reset_auth_store_info,
    select_provider,
)
from providers.openai_provider import (
    FAKE_KEY_MARKER,
    OPENAI_PROVIDER_ID,
    OpenAIProvider,
    api_key_credential,
    connect_openai_api_key,
    is_chat_model_id,
    validate_api_key_format,
)
from providers.registry import get_default_registry, reset_default_registry
from providers.status import assert_safe_provider_payload
from auth.store import AuthStoreInfo


class _FakeHandler(bridge.Handler):
    def __init__(self):
        self.client_address = ("127.0.0.1", 0)
        self._sent = {}

    def _send_json(self, status, obj):
        self._sent["status"] = status
        self._sent["body"] = obj
        return None


class OpenAIAuthTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        self.cred_path = Path(self.tmp.name) / "credentials.json"
        self.settings_path = Path(self.tmp.name) / "settings.json"
        self._old = bridge.SETTINGS_FILE
        bridge.SETTINGS_FILE = self.settings_path
        from providers import management as mgmt
        store = FileAuthStore(path=self.cred_path)
        mgmt._auth_info = AuthStoreInfo(
            store=store, backend="file",
            secure_cloud_auth_available=True, detail="test",
        )
        self.store = store

    def tearDown(self):
        bridge.SETTINGS_FILE = self._old
        reset_auth_store_info()
        reset_default_registry()
        self.tmp.cleanup()

    def test_validate_format(self):
        self.assertIsNotNone(validate_api_key_format(""))
        self.assertIsNotNone(validate_api_key_format("short"))
        self.assertIsNone(validate_api_key_format(FAKE_KEY_MARKER))

    def test_connect_stores_key_not_in_settings(self):
        prov = OpenAIProvider(transport=lambda *a, **k: (200, {"data": [{"id": "gpt-4o-mini"}]}))
        with mock.patch("providers.openai_provider.OpenAIProvider", return_value=prov):
            # Patch the class used inside connect_openai_api_key
            with mock.patch("providers.openai_provider.OpenAIProvider.validate_credential",
                            return_value={"ok": True, "validated": True}):
                result = connect_openai_api_key(self.store, FAKE_KEY_MARKER)
        self.assertTrue(result["credentialStored"])
        self.assertTrue(result["credentialValidated"])
        cred = self.store.load(OPENAI_PROVIDER_ID)
        self.assertEqual(cred.access_token, FAKE_KEY_MARKER)
        self.assertEqual(cred.metadata.get("credential_type"), "api_key")
        self.assertIsNone(cred.refresh_token)
        # settings untouched
        self.assertFalse(self.settings_path.exists() or "apiKey" in json.dumps(
            bridge.load_json(self.settings_path, {})
        ))

    def test_status_never_returns_key(self):
        self.store.save(OPENAI_PROVIDER_ID, api_key_credential(FAKE_KEY_MARKER, validated=True))
        status = get_provider_status(OPENAI_PROVIDER_ID, {"provider_id": "openai"})
        assert_safe_provider_payload(status)
        blob = json.dumps(status)
        self.assertNotIn(FAKE_KEY_MARKER, blob)
        self.assertNotIn("apiKey", blob)
        self.assertTrue(status["credentialStored"])
        self.assertTrue(status["authenticated"])

    def test_disconnect_removes_key(self):
        self.store.save(OPENAI_PROVIDER_ID, api_key_credential(FAKE_KEY_MARKER, validated=True))
        out = disconnect_provider(OPENAI_PROVIDER_ID, {})
        self.assertTrue(out["disconnected"])
        self.assertIsNone(self.store.load(OPENAI_PROVIDER_ID))

    def test_select_without_auth_fails(self):
        with self.assertRaises(AuthenticationRequired):
            select_provider(OPENAI_PROVIDER_ID, {}, save=lambda s: None)

    def test_invalid_key_validation_fails(self):
        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            raise AuthenticationRequired("bad", provider_id="openai")

        prov = OpenAIProvider(transport=transport)
        with self.assertRaises(AuthenticationRequired):
            connect_openai_api_key(self.store, FAKE_KEY_MARKER, provider=prov)
        self.assertIsNone(self.store.load(OPENAI_PROVIDER_ID))

    def test_temporary_validation_failure_keeps_key(self):
        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            raise ProviderUnavailable("down", provider_id="openai")

        prov = OpenAIProvider(transport=transport)
        with self.assertRaises(ProviderUnavailable):
            connect_openai_api_key(self.store, FAKE_KEY_MARKER, provider=prov)
        cred = self.store.load(OPENAI_PROVIDER_ID)
        self.assertIsNotNone(cred)
        self.assertEqual(cred.access_token, FAKE_KEY_MARKER)
        self.assertFalse(cred.metadata.get("credential_validated"))

    def test_credential_repr_hides_key(self):
        cred = api_key_credential(FAKE_KEY_MARKER, validated=True)
        rep = repr(cred)
        self.assertNotIn(FAKE_KEY_MARKER, rep)
        self.assertIn("has_access_token=True", rep)


class OpenAIModelsTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        from providers import management as mgmt
        self.store = FileAuthStore(path=Path(self.tmp.name) / "c.json")
        mgmt._auth_info = AuthStoreInfo(
            store=self.store, backend="file",
            secure_cloud_auth_available=True, detail="test",
        )
        self.store.save(OPENAI_PROVIDER_ID, api_key_credential(FAKE_KEY_MARKER, validated=True))

    def tearDown(self):
        reset_auth_store_info()
        reset_default_registry()
        self.tmp.cleanup()

    def test_filter_chat_models(self):
        self.assertTrue(is_chat_model_id("gpt-4o-mini"))
        self.assertFalse(is_chat_model_id("text-embedding-3-large"))
        self.assertFalse(is_chat_model_id("whisper-1"))

    def test_authenticated_listing(self):
        payload = {"data": [
            {"id": "gpt-4o-mini"},
            {"id": "text-embedding-3-small"},
            {"id": "gpt-4o"},
        ]}
        with mock.patch.object(OpenAIProvider, "_request_json", return_value=(200, payload)):
            out = list_models_for_provider("openai", {"openai_model": "gpt-4o-mini"})
        ids = {m["id"] for m in out["models"]}
        self.assertIn("gpt-4o-mini", ids)
        self.assertIn("gpt-4o", ids)
        self.assertNotIn("text-embedding-3-small", ids)
        assert_safe_provider_payload(out)
        self.assertNotIn(FAKE_KEY_MARKER, json.dumps(out))

    def test_unauthenticated_listing(self):
        self.store.delete(OPENAI_PROVIDER_ID)
        with self.assertRaises(AuthenticationRequired):
            list_models_for_provider("openai", {})

    def test_http_mapping(self):
        prov = OpenAIProvider()
        with self.assertRaises(AuthenticationRequired):
            raise prov._map_http_error(401, '{"error":{"message":"bad key sk-SECRET"}}')
        with self.assertRaises(AuthenticationRequired):
            raise prov._map_http_error(403, "forbidden")
        with self.assertRaises(RateLimited):
            raise prov._map_http_error(429, "slow")
        with self.assertRaises(ProviderUnavailable):
            raise prov._map_http_error(503, "down")


class OpenAIInferenceTest(unittest.TestCase):
    def test_stream_text_and_tools(self):
        chunks = [
            b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"read_file","arguments":"{\\"path\\":\\"a\\"}"}}]}}]}\n',
            b'data: {"usage":{"prompt_tokens":3,"completion_tokens":2},"choices":[]}\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n',
            b"data: [DONE]\n",
        ]

        class _Resp:
            def __init__(self):
                self._data = b"".join(chunks)
                self._i = 0

            def read(self, n=1024):
                if self._i >= len(self._data):
                    return b""
                out = self._data[self._i:self._i + n]
                self._i += n
                return out

            def close(self):
                return None

        prov = OpenAIProvider(transport=lambda *a, **k: (200, _Resp()))
        # open_chat_stream with transport stream path
        def transport(method, url, headers=None, body=None, timeout=None, stream=False):
            # Transport necessarily receives Authorization; ensure we don't
            # also leak the key into the JSON body.
            self.assertNotIn(FAKE_KEY_MARKER, json.dumps(body or {}))
            return (200, _Resp())

        prov = OpenAIProvider(transport=transport)
        from providers.base import InferenceRequest, InferenceEventType, RuntimeCredentials
        events = list(prov.stream_response(
            InferenceRequest(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]),
            RuntimeCredentials(provider_id="openai", access_token=FAKE_KEY_MARKER),
        ))
        types = [e.event_type for e in events]
        self.assertIn(InferenceEventType.TEXT_DELTA, types)
        self.assertIn(InferenceEventType.TOOL_CALL_DELTA, types)
        self.assertIn(InferenceEventType.COMPLETED, types)
        for e in events:
            self.assertTrue(e.raw is None)

    def test_registry_includes_openai(self):
        reset_default_registry()
        reg = get_default_registry()
        d = reg.get_definition("openai")
        self.assertTrue(d.enabled)
        self.assertTrue(d.experimental)
        self.assertEqual(d.auth_type.value, "api_key")


class OpenAIApiHandlerTest(unittest.TestCase):
    def setUp(self):
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        from providers import management as mgmt
        self.store = FileAuthStore(path=Path(self.tmp.name) / "c.json")
        mgmt._auth_info = AuthStoreInfo(
            store=self.store, backend="file",
            secure_cloud_auth_available=True, detail="test",
        )
        self._old = bridge.SETTINGS_FILE
        bridge.SETTINGS_FILE = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        bridge.SETTINGS_FILE = self._old
        reset_auth_store_info()
        reset_default_registry()
        self.tmp.cleanup()

    def test_connect_endpoint_safe(self):
        h = _FakeHandler()
        with mock.patch(
            "providers.openai_provider.OpenAIProvider.validate_credential",
            return_value={"ok": True, "validated": True},
        ):
            h._handle_providers_post(
                "/api/providers/openai/connect",
                {"apiKey": FAKE_KEY_MARKER},
            )
        self.assertEqual(h._sent["status"], 200)
        blob = json.dumps(h._sent["body"])
        self.assertNotIn(FAKE_KEY_MARKER, blob)
        assert_safe_provider_payload(h._sent["body"])

    def test_chat_requires_auth_when_openai_selected(self):
        h = _FakeHandler()
        with mock.patch.object(bridge, "get_settings", return_value={"provider_id": "openai"}):
            h._handle_chat({"message": "hi", "chat_id": "t"})
        self.assertEqual(h._sent["status"], 401)

    def test_ui_has_password_field(self):
        html = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="set-openai-key"', html)
        self.assertIn('type="password"', html)
        self.assertIn('autocomplete="off"', html)
        js = (Path(__file__).resolve().parent.parent / "app.js").read_text(encoding="utf-8")
        self.assertIn("set-openai-key", js)
        self.assertIn("input.value = \"\"", js)
        self.assertNotIn("localStorage.setItem(\"openai", js)
        self.assertNotIn(FAKE_KEY_MARKER, js)


if __name__ == "__main__":
    unittest.main()

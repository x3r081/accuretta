"""Local llama provider compatibility tests.

Uses fakes — never contacts a real llama-server. Verifies the provider
delegates to bridge helpers without changing settings semantics.

Run: python3 -m unittest tests.test_local_llama_provider -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.base import InferenceEventType, InferenceRequest
from providers.errors import ProviderNotConfigured, ProviderUnavailable
from providers.local_llama import LocalLlamaProvider, ensure_local_llama_registered
from providers.registry import get_default_registry, reset_default_registry


class _FakeBridge:
    def __init__(self):
        self.settings = {
            "model": "demo",
            "model_path": "/tmp/demo.gguf",
            "models_dir": "/tmp/models",
            "temperature": 0.7,
        }
        self._llama = SimpleNamespace(loaded_model=lambda: "/tmp/demo.gguf")
        self._chat_cancels = {}
        self._chat_cancels_lock = __import__("threading").Lock()
        self.ping_ok = True
        self.exists_ok = True
        self.scanned = [
            {"path": "/tmp/demo.gguf", "name": "demo.gguf", "size": 1},
            {"path": "/tmp/other.gguf", "name": "other.gguf", "size": 2},
        ]
        self.posted_payloads = []
        self.cancelled = []

    def get_settings(self):
        return dict(self.settings)

    def safe_exists(self, path):
        return self.exists_ok and bool(path)

    def llama_ping(self, timeout=1.0):
        return self.ping_ok

    def scan_gguf_dir(self, root):
        return list(self.scanned)

    def llama_get(self, path):
        return {"data": [{"id": "live-model"}]}

    def llama_options(self, settings):
        return {"temperature": settings.get("temperature", 0.7)}

    def cancel_chat(self, chat_id):
        self.cancelled.append(chat_id)
        return True

    def _llama_gen_idle_timeout_s(self):
        return 5.0

    def _llama_gen_wall_timeout_s(self):
        return 30.0

    def llama_post_stream(self, path, payload):
        self.posted_payloads.append(payload)

        class _Resp:
            def close(self):
                return None

        return _Resp()

    def iter_llama_sse(self, resp, *, idle_timeout, wall_timeout, cancel_ev=None):
        chunks = [
            b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
            b"data: [DONE]\n",
        ]
        for c in chunks:
            yield c


class LocalLlamaProviderTest(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeBridge()
        self.provider = LocalLlamaProvider(bridge_module=self.bridge)

    def tearDown(self):
        reset_default_registry()

    def test_validate_ok(self):
        self.provider.validate_configuration()  # no raise

    def test_validate_missing_model(self):
        self.bridge.settings["model_path"] = ""
        with self.assertRaises(ProviderNotConfigured):
            self.provider.validate_configuration()

    def test_validate_server_down(self):
        self.bridge.ping_ok = False
        with self.assertRaises(ProviderUnavailable):
            self.provider.validate_configuration()

    def test_list_models_from_scan(self):
        models = self.provider.list_models()
        self.assertEqual(len(models), 2)
        loaded = [m for m in models if m.get("loaded")]
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["path"], "/tmp/demo.gguf")
        self.assertEqual(models[0]["provider_id"], "local_llama")

    def test_list_models_fallback_to_live(self):
        self.bridge.scanned = []
        models = self.provider.list_models()
        self.assertEqual(models[0]["id"], "live-model")

    def test_cancel_delegates(self):
        self.assertTrue(self.provider.cancel("chat-1"))
        self.assertEqual(self.bridge.cancelled, ["chat-1"])

    def test_stream_emits_text_and_completion(self):
        req = InferenceRequest(
            model="demo",
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="be brief",
        )
        events = list(self.provider.stream_response(req))
        types = [e.event_type for e in events]
        self.assertIn(InferenceEventType.TEXT_DELTA, types)
        self.assertIn(InferenceEventType.COMPLETED, types)
        text = "".join(e.text_delta or "" for e in events)
        self.assertEqual(text, "Hi")
        payload = self.bridge.posted_payloads[0]
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertTrue(payload["stream"])

    def test_settings_keys_unchanged_contract(self):
        """Provider must read existing settings keys — no migration required."""
        for key in ("model", "model_path", "models_dir"):
            self.assertIn(key, self.bridge.get_settings())

    def test_factory_registered(self):
        reset_default_registry()
        ensure_local_llama_registered()
        provider = get_default_registry().get_provider("local_llama")
        self.assertIsInstance(provider, LocalLlamaProvider)
        self.assertEqual(provider.definition.id, "local_llama")
        self.assertEqual(provider.definition.auth_type.value, "none")


if __name__ == "__main__":
    unittest.main()

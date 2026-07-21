"""Tests for runtime credential resolution and safe status payloads.

Run: python3 -m unittest tests.test_providers_runtime -v
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.base import ApiMode, AuthType, ProviderDefinition
from providers.errors import AuthenticationExpired, AuthenticationRequired
from providers.registry import ProviderRegistry
from providers.runtime import RuntimeResolver, safe_provider_status


@dataclass
class _FakeCred:
    access_token: Optional[str]
    expires_at: Optional[float]
    base_url: Optional[str] = None
    source: str = "file"


class _FakeStore:
    def __init__(self, mapping):
        self.mapping = mapping

    def load(self, provider_id: str):
        return self.mapping.get(provider_id)


class RuntimeResolverTest(unittest.TestCase):
    def setUp(self):
        self.reg = ProviderRegistry()
        self.reg.register(
            ProviderDefinition(
                id="local_llama",
                display_name="Local llama.cpp",
                api_mode=ApiMode.LOCAL_LLAMA,
                auth_type=AuthType.NONE,
                default_base_url="http://127.0.0.1:8080",
            )
        )
        self.reg.register(
            ProviderDefinition(
                id="cloud",
                display_name="Cloud",
                api_mode=ApiMode.OPENAI_CHAT,
                auth_type=AuthType.OAUTH_PKCE,
                default_base_url="https://example.test",
            )
        )

    def test_local_default_needs_no_credentials(self):
        resolver = RuntimeResolver(self.reg, clock=lambda: 1_000_000.0)
        resolved = resolver.resolve()
        self.assertEqual(resolved.provider_id, "local_llama")
        self.assertIsNone(resolved.credentials.access_token)
        self.assertEqual(resolved.credentials.source, "local")
        self.assertEqual(resolved.credentials.base_url, "http://127.0.0.1:8080")

    def test_cloud_requires_auth(self):
        resolver = RuntimeResolver(self.reg, credential_source=_FakeStore({}))
        with self.assertRaises(AuthenticationRequired):
            resolver.resolve("cloud")

    def test_cloud_rejects_expired_within_skew(self):
        store = _FakeStore({
            "cloud": _FakeCred(access_token="tok", expires_at=1_000_050.0),
        })
        resolver = RuntimeResolver(
            self.reg,
            credential_source=store,
            refresh_skew_seconds=120,
            clock=lambda: 1_000_000.0,
        )
        with self.assertRaises(AuthenticationExpired):
            resolver.resolve("cloud")

    def test_cloud_accepts_fresh_token(self):
        store = _FakeStore({
            "cloud": _FakeCred(
                access_token="tok",
                expires_at=1_000_500.0,
                base_url="https://api.example.test",
                source="keychain",
            ),
        })
        resolver = RuntimeResolver(
            self.reg,
            credential_source=store,
            refresh_skew_seconds=120,
            clock=lambda: 1_000_000.0,
        )
        resolved = resolver.resolve("cloud")
        self.assertEqual(resolved.credentials.access_token, "tok")
        self.assertEqual(resolved.credentials.source, "keychain")
        self.assertEqual(resolved.credentials.base_url, "https://api.example.test")

    def test_safe_status_omits_tokens(self):
        d = self.reg.get_definition("cloud")
        status = safe_provider_status(
            d,
            authenticated=True,
            expires_at="2099-01-01T00:00:00Z",
            account_label="user@example.test",
        )
        blob = repr(status)
        self.assertNotIn("tok", blob)
        self.assertNotIn("access", blob.lower().replace("authenticated", ""))
        self.assertEqual(status["providerId"], "cloud")
        self.assertTrue(status["authenticated"])
        self.assertIn("streaming", status["capabilities"])
        self.assertIsNone(status["error"])


if __name__ == "__main__":
    unittest.main()

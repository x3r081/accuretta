"""Tests for the provider registry.

Run: python3 -m unittest tests.test_providers_registry -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.base import (
    ApiMode,
    AuthType,
    ProviderDefinition,
)
from providers.errors import ProviderNotConfigured
from providers.registry import ProviderRegistry, get_default_registry, reset_default_registry


class ProviderRegistryTest(unittest.TestCase):
    def tearDown(self):
        reset_default_registry()

    def test_register_and_list(self):
        reg = ProviderRegistry()
        reg.register(
            ProviderDefinition(
                id="local_llama",
                display_name="Local llama.cpp",
                api_mode=ApiMode.LOCAL_LLAMA,
                auth_type=AuthType.NONE,
            )
        )
        reg.register(
            ProviderDefinition(
                id="cloud_x",
                display_name="Cloud X",
                api_mode=ApiMode.OPENAI_CHAT,
                auth_type=AuthType.OAUTH_PKCE,
                experimental=True,
                enabled=False,
            )
        )
        enabled = reg.list_definitions()
        self.assertEqual([d.id for d in enabled], ["local_llama"])
        all_defs = reg.list_definitions(include_disabled=True)
        self.assertEqual({d.id for d in all_defs}, {"local_llama", "cloud_x"})

    def test_unknown_provider(self):
        reg = ProviderRegistry()
        with self.assertRaises(ProviderNotConfigured) as ctx:
            reg.get_definition("missing")
        self.assertEqual(ctx.exception.code, "provider_not_configured")

    def test_disabled_provider_factory_rejected(self):
        reg = ProviderRegistry()

        class _Stub:
            @property
            def definition(self):
                return ProviderDefinition(
                    id="x",
                    display_name="X",
                    api_mode=ApiMode.OPENAI_CHAT,
                    auth_type=AuthType.NONE,
                    enabled=False,
                )

        reg.register(
            ProviderDefinition(
                id="x",
                display_name="X",
                api_mode=ApiMode.OPENAI_CHAT,
                auth_type=AuthType.NONE,
                enabled=False,
            ),
            factory=lambda: _Stub(),
        )
        with self.assertRaises(ProviderNotConfigured):
            reg.get_provider("x")

    def test_default_registry_includes_local_llama(self):
        reset_default_registry()
        reg = get_default_registry()
        d = reg.get_definition("local_llama")
        self.assertEqual(d.display_name, "Local llama.cpp")
        self.assertEqual(d.auth_type, AuthType.NONE)
        self.assertTrue(d.capabilities.streaming)
        self.assertTrue(d.capabilities.tools)
        self.assertTrue(d.capabilities.vision)
        self.assertTrue(d.enabled)


if __name__ == "__main__":
    unittest.main()

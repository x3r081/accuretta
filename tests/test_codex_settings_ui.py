"""Settings UI + API: Codex provider selection (no silent auth→provider switch)."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex.discover import reset_discovery_cache
from codex.flags import ENV_CODEX_INFERENCE_ENABLED
from codex.session import reset_codex_session
from providers.codex_provider import (
    CODEX_PROVIDER_ID,
    get_codex_provider_status,
)
from providers.codex_readiness import (
    UI_REASON_INFERENCE_DISABLED,
    UI_REASON_NOT_SIGNED_IN,
    assess_codex_inference_readiness,
)
from providers.errors import ProviderUnavailable, AuthenticationRequired
from providers.management import (
    disconnect_provider,
    ensure_builtin_providers,
    list_provider_statuses,
    reset_auth_store_info,
    select_provider,
)
from providers.registry import reset_default_registry
from providers.status import assert_safe_provider_payload

FAKE_SERVER = Path(__file__).resolve().parent / "fake_codex_app_server.py"
SECRET = "sk-fake-SECRET-marker-do-not-leak"
ROOT = Path(__file__).resolve().parent.parent


def _make_fake_codex_bin(tmp: Path, *, mode: str = "authenticated") -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    wrapper = tmp / "codex"
    py = sys.executable
    script = FAKE_SERVER
    wrapper.write_text(
        "#!/bin/sh\n"
        f"export FAKE_CODEX_MODE={mode}\n"
        f"export FAKE_CODEX_SECRET_MARKER={SECRET}\n"
        f'exec "{py}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


class CodexSettingsUiContractTest(unittest.TestCase):
    def test_html_dropdown_and_copy(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="set-provider"', html)
        self.assertIn('id="provider-readiness"', html)
        self.assertIn("aria-describedby", html)
        self.assertIn("Authentication is connected here separately", html)
        self.assertIn("Accuretta never receives or stores ChatGPT OAuth tokens", html)
        self.assertIn("Codex inference is a separate capability", html)

    def test_js_exposes_codex_in_dropdown(self):
        js = (ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn("_codexDropdownLabel", js)
        self.assertIn("Codex via ChatGPT — uses your connected ChatGPT plan", js)
        self.assertIn("Local llama.cpp — default, local", js)
        self.assertIn("Provider status:", js)
        self.assertIn("Codex sign-in required", js)
        self.assertIn("example_cloud", js)  # explicitly skipped
        # Still skip account-only providers.
        self.assertIn("supportsInference === false", js)
        # Login / logout refresh form but must not auto-select.
        disconnect_idx = js.find("async function disconnectCodexFromUi")
        disconnect_chunk = js[disconnect_idx:disconnect_idx + 800]
        self.assertIn("populateProviderForm", disconnect_chunk)
        self.assertNotIn("/select", disconnect_chunk)
        login_idx = js.find("if (st === \"completed\" || res.authenticated)")
        login_chunk = js[login_idx - 400:login_idx + 200]
        self.assertIn("populateProviderForm", login_chunk)
        self.assertNotIn("/select", login_chunk)
        # No credential persistence in browser storage for Codex UI.
        start = js.find("async function populateCodexAuthForm")
        end = js.find("function _clearGitHubDeviceUi")
        chunk = js[start:end]
        self.assertNotIn("localStorage", chunk)
        self.assertNotIn("sessionStorage", chunk)
        self.assertNotIn("access_token", js)


class CodexSettingsSelectionApiTest(unittest.TestCase):
    def setUp(self):
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = _make_fake_codex_bin(Path(self.tmp.name), mode="authenticated")
        self._env = mock.patch.dict(
            os.environ,
            {
                "ACCURETTA_CODEX_BIN": str(self.bin),
                ENV_CODEX_INFERENCE_ENABLED: "1",
            },
            clear=False,
        )
        self._env.start()
        ensure_builtin_providers()

    def tearDown(self):
        try:
            reset_codex_session()
        except Exception:
            pass
        self._env.stop()
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        reset_auth_store_info()
        self.tmp.cleanup()

    def test_codex_option_enabled_when_ready(self):
        st = get_codex_provider_status(live=True)
        assert_safe_provider_payload(st)
        self.assertTrue(st["supportsInference"])
        self.assertTrue(st["selectable"])
        self.assertTrue(st["inferenceReady"])
        self.assertEqual(st["displayName"], "Codex via ChatGPT")
        self.assertEqual(st["indicator"]["kind"], "codex_ready")
        self.assertIsNone(st.get("selectionDisabledReason"))

    def test_disabled_when_unauthenticated(self):
        bin_u = _make_fake_codex_bin(Path(self.tmp.name) / "u", mode="ok")
        with mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(bin_u)}):
            reset_discovery_cache()
            reset_codex_session()
            ensure_builtin_providers()
            st = get_codex_provider_status(live=True)
        self.assertTrue(st["supportsInference"])
        self.assertFalse(st["selectable"])
        self.assertEqual(st["selectionDisabledReason"], UI_REASON_NOT_SIGNED_IN)
        self.assertEqual(st["indicator"]["kind"], "codex_sign_in_required")
        with self.assertRaises(AuthenticationRequired):
            select_provider(CODEX_PROVIDER_ID, {"provider_id": "local_llama"}, save=lambda s: None)

    def test_disabled_when_feature_flag_off(self):
        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            reset_discovery_cache()
            st = get_codex_provider_status(live=False)
            self.assertTrue(st["supportsInference"])
            self.assertFalse(st["selectable"])
            self.assertEqual(st["selectionDisabledReason"], UI_REASON_INFERENCE_DISABLED)
            self.assertEqual(st["indicator"]["kind"], "codex_disabled")
            with self.assertRaises(ProviderUnavailable) as ctx:
                select_provider(CODEX_PROVIDER_ID, {}, save=lambda s: None)
            self.assertEqual(ctx.exception.message, UI_REASON_INFERENCE_DISABLED)

    def test_selecting_codex_persists_provider_id_only(self):
        saves = []

        def save(settings):
            saves.append(dict(settings))

        settings = {"provider_id": "local_llama", "model": "x.gguf"}
        out = select_provider(CODEX_PROVIDER_ID, settings, save=save)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("providerId"), CODEX_PROVIDER_ID)
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0].get("provider_id"), CODEX_PROVIDER_ID)
        blob = json.dumps(saves[0])
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("access_token", blob)
        self.assertNotIn("refresh_token", blob)
        # Only provider id (+ existing settings); no account payload.
        self.assertNotIn("account", saves[0])
        self.assertNotIn("planType", saves[0])

    def test_disconnect_does_not_silently_switch_to_local(self):
        settings = {"provider_id": CODEX_PROVIDER_ID}
        saves = []

        def save(s):
            saves.append(dict(s))

        # Select Codex while ready.
        select_provider(CODEX_PROVIDER_ID, {"provider_id": "local_llama"}, save=save)
        self.assertEqual(saves[-1]["provider_id"], CODEX_PROVIDER_ID)

        # Disconnect auth — settings must not be rewritten to local_llama.
        result = disconnect_provider(CODEX_PROVIDER_ID, {"provider_id": CODEX_PROVIDER_ID})
        self.assertTrue(result.get("ok") or result.get("disconnected"))
        self.assertEqual(saves[-1]["provider_id"], CODEX_PROVIDER_ID)
        body = list_provider_statuses({"provider_id": CODEX_PROVIDER_ID})
        self.assertEqual(body["selectedProviderId"], CODEX_PROVIDER_ID)
        codex = next(p for p in body["providers"] if p["providerId"] == CODEX_PROVIDER_ID)
        self.assertFalse(codex.get("selectable"))
        self.assertEqual(codex.get("selectionDisabledReason"), UI_REASON_NOT_SIGNED_IN)

    def test_reconnect_restores_readiness(self):
        # Start unauthenticated.
        bin_u = _make_fake_codex_bin(Path(self.tmp.name) / "re", mode="ok")
        with mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(bin_u)}):
            reset_discovery_cache()
            reset_codex_session()
            r1 = assess_codex_inference_readiness(live=True)
            self.assertFalse(r1["ready"])
            self.assertEqual(r1["status"], "not_signed_in")
        # Authenticated fake restores readiness without changing provider_id.
        with mock.patch.dict(os.environ, {"ACCURETTA_CODEX_BIN": str(self.bin)}):
            reset_discovery_cache()
            reset_codex_session()
            r2 = assess_codex_inference_readiness(live=True)
            self.assertTrue(r2["ready"])
            st = get_codex_provider_status(live=True)
            self.assertTrue(st["selectable"])
        # Persisted selection remains whatever settings say.
        body = list_provider_statuses({"provider_id": CODEX_PROVIDER_ID})
        self.assertEqual(body["selectedProviderId"], CODEX_PROVIDER_ID)

    def test_persisted_unavailable_codex_preserved_in_list(self):
        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            reset_discovery_cache()
            body = list_provider_statuses({"provider_id": CODEX_PROVIDER_ID})
        self.assertEqual(body["selectedProviderId"], CODEX_PROVIDER_ID)
        codex = next(p for p in body["providers"] if p["providerId"] == CODEX_PROVIDER_ID)
        self.assertTrue(codex["supportsInference"])
        self.assertFalse(codex["selectable"])
        self.assertEqual(codex["selectionDisabledReason"], UI_REASON_INFERENCE_DISABLED)

    def test_example_cloud_hidden_from_inference_capability(self):
        ensure_builtin_providers()
        from providers.registry import get_default_registry
        demo = get_default_registry().get_definition("example_cloud")
        self.assertFalse(demo.supports_inference)


if __name__ == "__main__":
    unittest.main()

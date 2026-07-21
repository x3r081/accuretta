"""Default provider selection persistence (Settings) — UI + API contracts."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex.discover import reset_discovery_cache
from codex.flags import ENV_CODEX_INFERENCE_ENABLED
from codex.session import reset_codex_session
from providers.codex_provider import CODEX_PROVIDER_ID
from providers.errors import ProviderNotConfigured, ProviderUnavailable
from providers.management import (
    ensure_builtin_providers,
    list_provider_statuses,
    reset_auth_store_info,
    select_provider,
)
from providers.registry import reset_default_registry
from providers.selection import DEFAULT_PROVIDER_ID, persist_provider_id
from providers.session_binding import ensure_session_provider

ROOT = Path(__file__).resolve().parent.parent
FAKE_SERVER = Path(__file__).resolve().parent / "fake_codex_app_server.py"
SECRET = "sk-fake-SECRET-marker-do-not-leak"


def _make_fake_codex_bin(tmp: Path, *, mode: str = "authenticated") -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    wrapper = tmp / "codex"
    py = sys.executable
    wrapper.write_text(
        "#!/bin/sh\n"
        f"export FAKE_CODEX_MODE={mode}\n"
        f"export FAKE_CODEX_SECRET_MARKER={SECRET}\n"
        f'exec "{py}" "{FAKE_SERVER}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | 0o111)
    return wrapper


class DefaultProviderUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")

    def test_change_handler_does_not_rebuild_before_save(self):
        """Regression: populateProviderForm() before select wiped Codex choice."""
        idx = self.js.find('$("#set-provider")?.addEventListener("change"')
        self.assertGreater(idx, 0)
        chunk = self.js[idx : idx + 700]
        self.assertIn("selectProviderFromUi", chunk)
        self.assertIn("_pendingDefaultProviderId", chunk)
        # Executable call must not appear — a comment mentioning the bug is ok.
        self.assertIsNone(
            re.search(r"^\s*populateProviderForm\(\)", chunk, re.M),
            "change handler must not rebuild the select before save",
        )

    def test_dropdown_uses_settings_default_not_session(self):
        self.assertIn("function _settingsDefaultProviderId", self.js)
        # Only the head of populateProviderForm — later nested helpers are unrelated.
        pop = self.js.split("function populateProviderForm", 1)[1][:1200]
        self.assertIn("_settingsDefaultProviderId()", pop)
        self.assertIn("Never the open session", pop)
        # Must not assign selected from chat.inference_provider_id
        self.assertNotRegex(pop, r"selected\s*=\s*.*inference_provider_id")

    def test_select_uses_generation_guard_and_pending(self):
        sel = self.js.split("async function selectProviderFromUi", 1)[1].split(
            "async function disconnectProviderFromUi", 1
        )[0]
        self.assertIn("_providerSelectGen", sel)
        self.assertIn("_pendingDefaultProviderId", sel)
        self.assertIn("superseded by a newer selection", sel)
        self.assertIn('btnSelect.dataset.saving = "1"', sel)
        self.assertIn("Default provider updated", sel)

    def test_load_providers_skips_overwrite_while_pending(self):
        load = self.js.split("async function loadProviders", 1)[1].split(
            "function _providerSafeLabel", 1
        )[0]
        self.assertIn("if (!_pendingDefaultProviderId)", load)
        self.assertIn("if (gen !== _providerSelectGen) return", load)

    def test_settings_label_is_default_for_new_sessions(self):
        self.assertIn("Default inference provider (new sessions)", self.html)
        self.assertIn("provider-session-context", self.html)


class DefaultProviderPersistenceApiTest(unittest.TestCase):
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
        self._env.stop()
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        reset_auth_store_info()
        self.tmp.cleanup()

    def test_save_codex_as_default_persists_provider_id(self):
        saves = []
        out = select_provider(CODEX_PROVIDER_ID, {"provider_id": DEFAULT_PROVIDER_ID}, save=lambda s: saves.append(dict(s)))
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("providerId"), CODEX_PROVIDER_ID)
        self.assertEqual(saves[-1]["provider_id"], CODEX_PROVIDER_ID)

    def test_saved_codex_remains_selected_after_list_refresh(self):
        settings = {"provider_id": CODEX_PROVIDER_ID}
        body = list_provider_statuses(settings)
        self.assertEqual(body["selectedProviderId"], CODEX_PROVIDER_ID)
        # Local session state is not part of this API — selected is Settings only.
        self.assertNotIn("inference_provider_id", body)

    def test_saved_codex_survives_settings_roundtrip(self):
        settings = {"provider_id": DEFAULT_PROVIDER_ID, "model": "qwen"}
        updated = persist_provider_id(settings, CODEX_PROVIDER_ID)
        # Simulate restart: only the settings dict is reloaded.
        reloaded = dict(updated)
        body = list_provider_statuses(reloaded)
        self.assertEqual(body["selectedProviderId"], CODEX_PROVIDER_ID)
        self.assertEqual(reloaded["provider_id"], CODEX_PROVIDER_ID)

    def test_local_session_does_not_overwrite_codex_default(self):
        settings = {"provider_id": CODEX_PROVIDER_ID}
        chat = {"id": "local-open", "messages": []}
        ensure_session_provider(chat, {"provider_id": DEFAULT_PROVIDER_ID}, for_new_chat=True)
        self.assertEqual(chat["inference_provider_id"], DEFAULT_PROVIDER_ID)
        # Settings default unchanged by session bind.
        body = list_provider_statuses(settings)
        self.assertEqual(body["selectedProviderId"], CODEX_PROVIDER_ID)
        self.assertEqual(settings["provider_id"], CODEX_PROVIDER_ID)

    def test_unavailable_codex_preserves_saved_preference(self):
        settings = {"provider_id": CODEX_PROVIDER_ID}
        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            reset_discovery_cache()
            body = list_provider_statuses(settings)
        self.assertEqual(body["selectedProviderId"], CODEX_PROVIDER_ID)
        codex = next(p for p in body["providers"] if p["providerId"] == CODEX_PROVIDER_ID)
        self.assertFalse(codex.get("selectable"))
        # Must not rewrite settings
        self.assertEqual(settings["provider_id"], CODEX_PROVIDER_ID)

    def test_new_session_inherits_saved_codex_default(self):
        settings = {"provider_id": CODEX_PROVIDER_ID}
        chat = {"id": "new1", "messages": []}
        pid, label, notice = ensure_session_provider(chat, settings, for_new_chat=True)
        self.assertEqual(pid, CODEX_PROVIDER_ID)
        self.assertIn("Codex", label)
        self.assertIsNone(notice)

    def test_existing_local_session_remains_local(self):
        settings = {"provider_id": CODEX_PROVIDER_ID}
        chat = {"id": "old", "messages": []}
        ensure_session_provider(chat, {"provider_id": DEFAULT_PROVIDER_ID}, for_new_chat=True)
        pid, _, notice = ensure_session_provider(chat, settings, for_new_chat=False)
        self.assertEqual(pid, DEFAULT_PROVIDER_ID)
        self.assertIsNotNone(notice)
        self.assertIn("Local llama.cpp", notice)
        self.assertIn("Codex via ChatGPT", notice)

    def test_switch_back_to_local_persists(self):
        saves = []
        select_provider(CODEX_PROVIDER_ID, {"provider_id": DEFAULT_PROVIDER_ID}, save=lambda s: saves.append(dict(s)))
        out = select_provider(DEFAULT_PROVIDER_ID, saves[-1], save=lambda s: saves.append(dict(s)))
        self.assertTrue(out.get("ok"))
        self.assertEqual(saves[-1]["provider_id"], DEFAULT_PROVIDER_ID)

    def test_unknown_provider_id_rejected_on_select(self):
        with self.assertRaises(ProviderNotConfigured):
            select_provider("not_a_real_provider", {"provider_id": DEFAULT_PROVIDER_ID}, save=lambda s: None)

    def test_select_does_not_silently_fall_back_to_local_when_codex_blocked(self):
        with mock.patch.dict(os.environ, {ENV_CODEX_INFERENCE_ENABLED: "0"}):
            reset_discovery_cache()
            with self.assertRaises(ProviderUnavailable) as ctx:
                select_provider(CODEX_PROVIDER_ID, {"provider_id": DEFAULT_PROVIDER_ID}, save=lambda s: None)
            self.assertEqual(ctx.exception.provider_id, CODEX_PROVIDER_ID)


class DefaultProviderAccountRefreshTest(unittest.TestCase):
    """Account / readiness refresh must not rewrite settings.provider_id."""

    def setUp(self):
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        reset_auth_store_info()
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = _make_fake_codex_bin(Path(self.tmp.name), mode="authenticated")
        self._env = mock.patch.dict(
            os.environ,
            {"ACCURETTA_CODEX_BIN": str(self.bin), ENV_CODEX_INFERENCE_ENABLED: "1"},
            clear=False,
        )
        self._env.start()
        ensure_builtin_providers()

    def tearDown(self):
        self._env.stop()
        reset_codex_session()
        reset_discovery_cache()
        reset_default_registry()
        reset_auth_store_info()
        self.tmp.cleanup()

    def test_list_refresh_does_not_mutate_settings_dict(self):
        settings = {"provider_id": CODEX_PROVIDER_ID, "model": "qwen"}
        before = dict(settings)
        list_provider_statuses(settings)
        list_provider_statuses(settings)
        self.assertEqual(settings, before)

    def test_js_sse_refresh_uses_loadProviders_not_session(self):
        js = (ROOT / "app.js").read_text(encoding="utf-8")
        # providers:update / settings:update re-load providers then populate form —
        # pending-guard in loadProviders prevents clobbering an in-flight save.
        self.assertIn('evt.type === "providers:update"', js)
        self.assertIn('evt.type === "settings:update"', js)
        self.assertIn("_pendingDefaultProviderId", js)


if __name__ == "__main__":
    unittest.main()

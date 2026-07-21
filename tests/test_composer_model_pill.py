"""Composer model pill: session-bound Local selector vs Codex indicator."""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent


class ComposerModelPillContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "app.css").read_text(encoding="utf-8")
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")

    def _wire_chunk(self) -> str:
        return self.js.split("function wireModelMenu", 1)[1].split(
            "function applyMobileTab", 1
        )[0]

    def _render_chunk(self) -> str:
        return self.js.split("function renderModelPill", 1)[1].split(
            "async function refreshModels", 1
        )[0]

    def test_local_session_renders_enabled_selector(self):
        chunk = self._render_chunk()
        self.assertIn('dataset.mode = "local"', chunk)
        self.assertIn('aria-haspopup", "listbox"', chunk)
        self.assertIn("click to change model", chunk)
        self.assertIn("independent of Settings default", chunk)

    def test_click_handler_uses_btn_not_undefined_pill(self):
        """Regression: undefined `pill` threw on every click (dead Qwen selector)."""
        chunk = self._wire_chunk()
        self.assertIn("btn.classList.contains(\"is-provider-locked\")", chunk)
        self.assertIn("btn.dataset.mode !== \"local\"", chunk)
        # Must not reference bare `pill.` in the click/toggle path.
        self.assertIsNone(re.search(r"(?<![\w.])pill\.", chunk))

    def test_local_selector_opens_on_click_and_keyboard(self):
        chunk = self._wire_chunk()
        self.assertIn("function openMenu", chunk)
        self.assertIn("function toggleMenuFromUser", chunk)
        self.assertIn('e.key !== "Enter"', chunk)
        self.assertIn('e.key !== " "', chunk)
        self.assertIn("toggleMenuFromUser()", chunk)

    def test_codex_session_renders_indicator_not_qwen(self):
        chunk = self._render_chunk()
        self.assertIn('mode: "codex"', chunk)
        self.assertIn("Codex via ChatGPT", chunk)
        self.assertIn("Never invent GPT-4", chunk)
        self.assertIn("model-pill-indicator", chunk)
        self.assertIn(
            "Start a new Local llama.cpp session to choose a local model.",
            chunk,
        )
        # Must not set local GGUF path into the Codex indicator branch.
        codex_branch = chunk.split('mode: "codex"', 1)[1].split('mode: "openai"', 1)[0]
        self.assertNotIn("model_path", codex_branch)
        self.assertNotIn("loadedModel", codex_branch)

    def test_codex_indicator_does_not_open_local_menu(self):
        chunk = self._wire_chunk()
        self.assertIn("indicator only", chunk)
        self.assertIn("never open the local list", chunk)
        self.assertIn("closeMenu()", chunk)

    def test_session_not_settings_drives_composer(self):
        sess = self.js.split("function _sessionInferenceProviderId", 1)[1].split(
            "function _sessionInferenceProviderLabel", 1
        )[0]
        self.assertNotIn("_activeInferenceProviderId()", sess)
        self.assertIn('return "local_llama"', sess)
        render = self._render_chunk()
        self.assertIn("_sessionInferenceProviderId()", render)
        self.assertNotIn("_activeInferenceProviderId()", render)

    def test_loading_placeholder_not_qwen(self):
        chunk = self._render_chunk()
        self.assertIn('mode: "loading"', chunk)
        self.assertIn("Loading session…", chunk)
        self.assertIn('label: "…"', chunk)

    def test_css_locked_not_opacity_only(self):
        self.assertIn(".model-pill.model-pill-indicator", self.css)
        self.assertIn("Do not rely on opacity alone", self.css)
        self.assertIn('.model-pill[data-mode="local"]', self.css)
        self.assertIn("cursor: pointer", self.css)

    def test_header_composer_share_session_helper(self):
        self.assertIn("function updateChatMeta", self.js)
        self.assertIn("function refreshSessionProviderUI", self.js)
        refresh = self.js.split("function refreshSessionProviderUI", 1)[1][:400]
        self.assertIn("updateChatMeta()", refresh)
        self.assertIn("renderModelPill()", refresh)

    def test_select_chat_refreshes_composer(self):
        # Session switch must refresh provider chrome (pill + header).
        self.assertIn("refreshSessionProviderUI()", self.js)
        select = self.js.split("function selectChat", 1)[1].split(
            "function refreshSessionDesktopState", 1
        )[0]
        self.assertIn("refreshSessionProviderUI()", select)


class ComposerPillIntegrationAgreementTest(unittest.TestCase):
    """Header / composer / binding agree on session provider (no routing change)."""

    def test_legacy_missing_metadata_is_local_for_composer(self):
        from providers.session_binding import ensure_session_provider

        chat = {"id": "leg", "messages": [{"role": "user", "content": "hi"}]}
        pid, label, _ = ensure_session_provider(
            chat, {"provider_id": "codex_chatgpt"}, for_new_chat=False
        )
        self.assertEqual(pid, "local_llama")
        self.assertIn("Local", label)

    def test_new_codex_session_label_for_indicator(self):
        from providers.session_binding import ensure_session_provider, provider_display_name

        chat = {"id": "n", "messages": []}
        pid, label, _ = ensure_session_provider(
            chat, {"provider_id": "codex_chatgpt"}, for_new_chat=True
        )
        self.assertEqual(pid, "codex_chatgpt")
        self.assertEqual(label, provider_display_name("codex_chatgpt"))
        self.assertEqual(label, "Codex via ChatGPT")


if __name__ == "__main__":
    unittest.main()

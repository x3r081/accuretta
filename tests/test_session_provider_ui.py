"""Frontend UI contracts for session-bound provider chrome."""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = Path(__file__).resolve().parent.parent


class SessionProviderUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")

    def test_header_uses_session_provider_meta(self):
        self.assertIn('id="chat-meta"', self.html)
        self.assertIn("function updateChatMeta", self.js)
        self.assertIn("refreshSessionProviderUI", self.js)
        # Must not hard-code "local" as the initial header claim.
        meta = re.search(r'id="chat-meta"[^>]*>[^<]*<', self.html)
        self.assertIsNotNone(meta)
        self.assertNotIn(">local<", meta.group(0))

    def test_local_session_keeps_model_selector(self):
        self.assertIn('id="model-pill"', self.html)
        self.assertIn('dataset.mode = "local"', self.js)
        self.assertIn("click to change model", self.js)

    def test_codex_session_shows_provider_indicator_not_gguf_selector(self):
        self.assertIn('mode: "codex"', self.js)
        self.assertIn("is-provider-locked", self.js)
        self.assertIn("This session is bound to Codex", self.js)
        self.assertIn("Start a new Local llama.cpp session to choose a local model", self.js)
        # Locked pill must not open the local model menu.
        self.assertIn('btn.dataset.mode !== "local"', self.js)

    def test_assistant_label_from_response_metadata(self):
        self.assertIn("function assistantResponseLabel", self.js)
        self.assertIn("Never invent cloud model names", self.js)
        self.assertIn("m.model_label", self.js)
        self.assertIn("assistantResponseLabel(m)", self.js)
        # Must not fall back to global Settings model for labelled assistant rows
        # via the old `state.settings.model || "agent"` pattern in renderBubble.
        render_fn = self.js.split("function renderBubble", 1)[1].split(
            "function enhanceCodeBlocks", 1
        )[0]
        self.assertIn("assistantResponseLabel(m)", render_fn)
        self.assertNotIn('state.settings.model || "agent"', render_fn)

    def test_mismatch_message_and_new_session_action(self):
        self.assertIn("New sessions will use", self.js)
        self.assertIn("Start new Codex session", self.js)
        self.assertIn("startNewSessionForSettingsProvider", self.js)
        self.assertIn('id="btn-provider-new-session"', self.html)
        self.assertIn('id="provider-session-banner"', self.html)

    def test_settings_distinguishes_default_vs_open_session(self):
        self.assertIn("Default inference provider (new sessions)", self.html)
        self.assertIn('id="provider-session-context"', self.html)
        self.assertIn("Default for new sessions:", self.js)
        self.assertIn("This session:", self.js)
        self.assertIn("function updateProviderSessionContext", self.js)

    def test_no_flash_before_session_loads(self):
        # Empty pill name until metadata resolves; loading mode avoids Qwen flash.
        pill = re.search(
            r'id="model-pill"[^>]*>.*?model-pill-name[^>]*>[^<]*<',
            self.html,
            re.S,
        )
        self.assertIsNotNone(pill)
        self.assertNotRegex(pill.group(0), r"qwen|select model", re.I)
        self.assertIn('mode: "loading"', self.js)
        self.assertIn("Avoid flashing the local Qwen name", self.js)

    def test_legacy_missing_metadata_is_local(self):
        self.assertIn("Legacy migration: missing metadata ⇒ Local", self.js)
        self.assertIn('return "local_llama"', self.js)
        # Must not infer session provider from Settings when metadata is missing.
        sess_fn = self.js.split("function _sessionInferenceProviderId", 1)[1].split(
            "function _sessionInferenceProviderLabel", 1
        )[0]
        self.assertNotIn("_activeInferenceProviderId()", sess_fn)

    def test_unavailable_codex_stays_labelled_codex(self):
        self.assertIn("function _codexSessionUnavailableReason", self.js)
        self.assertIn("will not fall back to local", self.js)
        self.assertIn('id="provider-session-unavailable"', self.html)
        self.assertIn("Existing Codex sessions stay labelled Codex", self.js)
        # Indicator still uses Codex label even when unavailable.
        self.assertIn('classList.toggle("is-unavailable"', self.js)

    def test_no_false_gpt4_claim(self):
        label_fn = self.js.split("function assistantResponseLabel", 1)[1].split(
            "function updateChatMeta", 1
        )[0]
        self.assertIn("Never invent cloud model names", label_fn)
        self.assertNotRegex(label_fn, r'["\']GPT-4|["\']gpt-4', re.I)
        # Codex pill indicator uses provider display name, not a guessed model.
        self.assertIn('mode: "codex"', self.js)
        self.assertIn("Never invent GPT-4", self.js)
        self.assertIn(
            'const base = _sessionInferenceProviderLabel() || "Codex via ChatGPT";',
            self.js,
        )
        self.assertIn("model-pill-indicator", self.js)


class SessionProviderUiIntegrationTest(unittest.TestCase):
    """Light integration: mismatch copy + legacy bind agree with UI strings."""

    def test_mismatch_notice_matches_ui_copy(self):
        from providers.session_binding import mismatch_notice

        note = mismatch_notice("local_llama", "codex_chatgpt")
        self.assertEqual(
            note,
            "This session uses Local llama.cpp. New sessions will use Codex via ChatGPT.",
        )

    def test_legacy_unbound_chat_is_local_not_settings(self):
        from providers.session_binding import ensure_session_provider

        chat = {"id": "legacy", "messages": [{"role": "user", "content": "hi"}]}
        pid, label, notice = ensure_session_provider(
            chat, {"provider_id": "codex_chatgpt"}, for_new_chat=False
        )
        self.assertEqual(pid, "local_llama")
        self.assertIn("Local", label)
        self.assertIsNotNone(notice)
        self.assertIn("New sessions will use Codex via ChatGPT", notice)


if __name__ == "__main__":
    unittest.main()

"""Credential store tests (file backend + redaction + concurrency).

Run: python3 -m unittest tests.test_auth_store -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.file_store import FileAuthStore
from auth.models import StoredCredential
from auth.redact import mask_secret, redact_mapping, redact_sensitive_text, token_fingerprint
from auth.store import create_auth_store


class FileAuthStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "credentials.json"
        self.store = FileAuthStore(path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_load_delete(self):
        cred = StoredCredential(
            provider_id="fake",
            access_token="access-secret",
            refresh_token="refresh-secret",
            expires_at=2_000_000_000.0,
            scopes=["a", "b"],
            metadata={"account_label": "tester"},
        )
        self.store.save("fake", cred)
        loaded = self.store.load("fake")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.access_token, "access-secret")
        self.assertEqual(loaded.source, "file")
        self.assertEqual(self.store.list_authenticated_providers(), ["fake"])
        self.assertTrue(self.store.delete("fake"))
        self.assertIsNone(self.store.load("fake"))
        public = cred.safe_public_view()
        self.assertNotIn("access_token", public)
        self.assertNotIn("refresh_token", public)

    def test_owner_only_permissions(self):
        if os.name == "nt":
            self.skipTest("POSIX mode bits not enforced on Windows")
        cred = StoredCredential(provider_id="fake", access_token="tok")
        self.store.save("fake", cred)
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_malformed_stored_data(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text('{"version":1,"providers":{"fake":{"nope":true}}}\n', encoding="utf-8")
        self.assertIsNone(self.store.load("fake"))

    def test_corrupt_file_starts_empty(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not-json", encoding="utf-8")
        self.assertEqual(self.store.list_authenticated_providers(), [])
        corrupt = list(self.path.parent.glob("*.corrupt"))
        self.assertTrue(corrupt)

    def test_concurrent_writes(self):
        def write_one(i: int):
            s = FileAuthStore(path=self.path)
            s.save(
                f"p{i}",
                StoredCredential(provider_id=f"p{i}", access_token=f"tok-{i}"),
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write_one, range(20)))
        ids = self.store.list_authenticated_providers()
        self.assertEqual(len(ids), 20)
        for i in range(20):
            cred = self.store.load(f"p{i}")
            self.assertIsNotNone(cred)
            assert cred is not None
            self.assertEqual(cred.access_token, f"tok-{i}")

    def test_create_auth_store_force_file(self):
        info = create_auth_store(file_path=self.path, force_file=True)
        self.assertEqual(info.backend, "file")
        self.assertTrue(info.secure_cloud_auth_available)


class RedactionTest(unittest.TestCase):
    def test_redact_text_and_mapping(self):
        text = "Authorization: Bearer super-secret-token"
        self.assertIn("[REDACTED]", redact_sensitive_text(text))
        self.assertNotIn("super-secret-token", redact_sensitive_text(text))
        blob = {"access_token": "abc", "nested": {"refresh_token": "xyz"}, "ok": "fine"}
        red = redact_mapping(blob)
        self.assertEqual(red["access_token"], "[REDACTED]")
        self.assertEqual(red["nested"]["refresh_token"], "[REDACTED]")
        self.assertEqual(red["ok"], "fine")
        self.assertNotEqual(token_fingerprint("abc"), "abc")
        self.assertIn("***", mask_secret("short") or mask_secret("abcdefghij"))


if __name__ == "__main__":
    unittest.main()

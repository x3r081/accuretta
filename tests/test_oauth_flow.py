"""Fake OAuth PKCE / loopback / refresh tests.

Uses only the in-process FakeOAuthServer — never a real provider.

Run: python3 -m unittest tests.test_oauth_flow -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.file_store import FileAuthStore
from auth.loopback import LoopbackListener
from auth.models import StoredCredential
from auth.oauth_client import (
    OAuthProviderConfig,
    begin_authorization,
    exchange_authorization_code,
    poll_device_code_token,
    refresh_access_token,
)
from auth.pkce import generate_state, pkce_pair
from auth.token_refresh import ensure_fresh_credential, is_expiring
from providers.errors import (
    AuthenticationCancelled,
    AuthenticationExpired,
    OAuthStateMismatch,
    TokenExchangeFailed,
    TokenRefreshFailed,
)
from tests.fake_oauth_server import FakeOAuthServer


class OAuthFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = Path(self.tmp.name) / "credentials.json"
        self.store = FileAuthStore(path=self.store_path)
        self.server = FakeOAuthServer()
        self.server.start()
        self.config = OAuthProviderConfig(
            provider_id="fake_oauth",
            authorize_url=self.server.authorize_url,
            token_url=self.server.token_url,
            client_id=self.server.client_id,
            scopes=("test",),
        )

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def _drive_callback(self, redirect_uri: str, url: str) -> None:
        # Give the listener a moment to bind/serve.
        time.sleep(0.05)
        try:
            urllib.request.urlopen(url, timeout=5).read()
        except urllib.error.HTTPError:
            # Expected when the listener rejects state/code/error callbacks.
            pass

    def test_successful_pkce_browser_flow(self):
        pair = pkce_pair()
        state = generate_state()
        listener = LoopbackListener(timeout_seconds=5.0, expected_state=state)
        redirect_uri = listener.start()
        session = begin_authorization(
            self.config, redirect_uri=redirect_uri, state=state, pkce=pair
        )
        self.assertIn("code_challenge=", session.authorize_url)
        self.assertIn("state=", session.authorize_url)

        code = self.server.issue_code(redirect_uri=redirect_uri, challenge=pair.challenge)
        cb = self.server.callback_url(redirect_uri, code=code, state=state)
        threading.Thread(target=self._drive_callback, args=(redirect_uri, cb), daemon=True).start()
        result = listener.wait()
        self.assertEqual(result.code, code)
        self.assertEqual(result.state, state)

        token = exchange_authorization_code(
            self.config,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=pair.verifier,
        )
        self.assertTrue(token.access_token.startswith("access-"))
        self.assertTrue(token.refresh_token)
        self.store.save(
            "fake_oauth",
            StoredCredential(
                provider_id="fake_oauth",
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                expires_at=time.time() + 3600,
                scopes=["test"],
            ),
        )
        self.assertIsNotNone(self.store.load("fake_oauth"))

    def test_reject_duplicated_parameters(self):
        state = generate_state()
        listener = LoopbackListener(timeout_seconds=5.0, expected_state=state)
        redirect_uri = listener.start()
        # Manually craft duplicated code=
        url = f"{redirect_uri}?code=a&code=b&state={state}"
        threading.Thread(target=self._drive_callback, args=(redirect_uri, url), daemon=True).start()
        with self.assertRaises(Exception):
            listener.wait()

    def test_reject_incorrect_state(self):
        state = generate_state()
        listener = LoopbackListener(timeout_seconds=5.0, expected_state=state)
        redirect_uri = listener.start()
        cb = self.server.callback_url(redirect_uri, code="c", state="wrong-state")
        threading.Thread(target=self._drive_callback, args=(redirect_uri, cb), daemon=True).start()
        with self.assertRaises(OAuthStateMismatch):
            listener.wait()

    def test_reject_missing_code(self):
        state = generate_state()
        listener = LoopbackListener(timeout_seconds=5.0, expected_state=state)
        redirect_uri = listener.start()
        cb = self.server.callback_url(redirect_uri, state=state)
        threading.Thread(target=self._drive_callback, args=(redirect_uri, cb), daemon=True).start()
        with self.assertRaises(Exception):
            listener.wait()

    def test_provider_error_callback(self):
        state = generate_state()
        listener = LoopbackListener(timeout_seconds=5.0, expected_state=state)
        redirect_uri = listener.start()
        cb = self.server.callback_url(
            redirect_uri, state=state, error="access_denied", error_description="nope"
        )
        threading.Thread(target=self._drive_callback, args=(redirect_uri, cb), daemon=True).start()
        with self.assertRaises(AuthenticationCancelled):
            listener.wait()

    def test_callback_timeout_and_cleanup(self):
        listener = LoopbackListener(timeout_seconds=0.4, expected_state="s")
        redirect_uri = listener.start()
        self.assertTrue(redirect_uri.startswith("http://127.0.0.1:"))
        with self.assertRaises(TimeoutError):
            listener.wait()
        # Port should be released — binding again must succeed.
        listener2 = LoopbackListener(timeout_seconds=0.2)
        uri2 = listener2.start()
        listener2.close()
        self.assertTrue(uri2.startswith("http://127.0.0.1:"))

    def test_token_exchange_failure(self):
        self.server.force_token_error = "invalid_client"
        with self.assertRaises(TokenExchangeFailed):
            exchange_authorization_code(
                self.config,
                code="nope",
                redirect_uri="http://127.0.0.1:9/callback",
                code_verifier="verifier",
            )

    def test_successful_refresh_and_rotation(self):
        pair = pkce_pair()
        redirect_uri = "http://127.0.0.1:9/callback"
        code = self.server.issue_code(redirect_uri=redirect_uri, challenge=pair.challenge)
        token = exchange_authorization_code(
            self.config,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=pair.verifier,
        )
        refreshed = refresh_access_token(self.config, refresh_token=token.refresh_token)
        self.assertNotEqual(refreshed.access_token, token.access_token)
        self.assertNotEqual(refreshed.refresh_token, token.refresh_token)
        # Old refresh token must not work after rotation.
        with self.assertRaises(TokenRefreshFailed):
            refresh_access_token(self.config, refresh_token=token.refresh_token)

    def test_refresh_failure_clears_credentials(self):
        self.store.save(
            "fake_oauth",
            StoredCredential(
                provider_id="fake_oauth",
                access_token="stale",
                refresh_token="bad-refresh",
                expires_at=time.time() - 10,
            ),
        )
        with self.assertRaises(TokenRefreshFailed):
            ensure_fresh_credential(self.store, self.config, skew_seconds=120)
        self.assertIsNone(self.store.load("fake_oauth"))

    def test_expiry_skew(self):
        now = 1_000_000.0
        self.assertTrue(is_expiring(now + 60, skew_seconds=120, now=now))
        self.assertFalse(is_expiring(now + 600, skew_seconds=120, now=now))
        self.assertTrue(is_expiring(None, skew_seconds=120, now=now))

    def test_logout_removes_credentials(self):
        self.store.save(
            "fake_oauth",
            StoredCredential(provider_id="fake_oauth", access_token="x", refresh_token="y"),
        )
        self.assertTrue(self.store.delete("fake_oauth"))
        self.assertIsNone(self.store.load("fake_oauth"))
        self.assertEqual(self.store.list_authenticated_providers(), [])

    def test_ensure_fresh_without_refresh_raises_expired(self):
        self.store.save(
            "fake_oauth",
            StoredCredential(
                provider_id="fake_oauth",
                access_token="stale",
                refresh_token=None,
                expires_at=time.time() - 1,
            ),
        )
        with self.assertRaises(AuthenticationExpired):
            ensure_fresh_credential(self.store, self.config)
        self.assertIsNone(self.store.load("fake_oauth"))

    def test_device_code_polling_primitive(self):
        calls = {"n": 0}

        def post_form(url, data):
            calls["n"] += 1
            if calls["n"] < 3:
                return {"error": "authorization_pending"}
            if calls["n"] == 3:
                return {"error": "slow_down"}
            return {
                "access_token": "device-access",
                "refresh_token": "device-refresh",
                "expires_in": 60,
                "token_type": "Bearer",
            }

        sleeps = []
        clock = {"t": 0.0}

        token = poll_device_code_token(
            token_url="http://example.test/token",
            client_id="cid",
            device_code="dc",
            expires_in=30,
            interval=1,
            sleep=lambda s: sleeps.append(s) or clock.__setitem__("t", clock["t"] + s),
            monotonic=lambda: clock["t"],
            post_form=post_form,
        )
        self.assertEqual(token.access_token, "device-access")
        self.assertTrue(sleeps)


if __name__ == "__main__":
    unittest.main()

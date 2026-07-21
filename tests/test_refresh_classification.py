"""Refresh failure classification tests.

Run: python3 -m unittest tests.test_refresh_classification -v
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.file_store import FileAuthStore
from auth.models import StoredCredential
from auth.oauth_client import OAuthProviderConfig, TokenResponse, refresh_access_token
from auth.refresh_errors import (
    RefreshFailureKind,
    classify_refresh_http_failure,
    should_delete_credentials,
)
from auth.token_refresh import ensure_fresh_credential
from providers.errors import (
    AuthenticationRequired,
    ProviderNotConfigured,
    TokenRefreshFailed,
)


class ClassifyRefreshTest(unittest.TestCase):
    def test_permanent_oauth_errors(self):
        for err in ("invalid_grant", "invalid_client", "revoked", "expired_token"):
            self.assertEqual(
                classify_refresh_http_failure(oauth_error=err, status_code=400),
                RefreshFailureKind.PERMANENT,
            )

    def test_transient_http(self):
        self.assertEqual(
            classify_refresh_http_failure(status_code=429),
            RefreshFailureKind.TRANSIENT,
        )
        self.assertEqual(
            classify_refresh_http_failure(status_code=500),
            RefreshFailureKind.TRANSIENT,
        )
        self.assertEqual(
            classify_refresh_http_failure(status_code=503),
            RefreshFailureKind.TRANSIENT,
        )

    def test_transient_transport(self):
        self.assertEqual(
            classify_refresh_http_failure(transport_exc=TimeoutError("x")),
            RefreshFailureKind.TRANSIENT,
        )
        self.assertEqual(
            classify_refresh_http_failure(
                transport_exc=urllib.error.URLError("dns")
            ),
            RefreshFailureKind.TRANSIENT,
        )


class EnsureFreshCredentialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FileAuthStore(path=Path(self.tmp.name) / "credentials.json")
        self.config = OAuthProviderConfig(
            provider_id="fake_oauth",
            authorize_url="http://127.0.0.1/authorize",
            token_url="http://127.0.0.1/token",
            client_id="cid",
        )
        self.store.save(
            "fake_oauth",
            StoredCredential(
                provider_id="fake_oauth",
                access_token="stale-access",
                refresh_token="rt-keep-me",
                expires_at=1.0,  # expired
            ),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _ensure(self, refresher):
        return ensure_fresh_credential(
            self.store,
            self.config,
            skew_seconds=120,
            now=1000.0,
            refresher=refresher,
        )

    def test_invalid_grant_deletes_credentials(self):
        def bad(config, **kwargs):
            raise TokenRefreshFailed(
                "rejected",
                provider_id="fake_oauth",
                kind="permanent",
                retryable=False,
                oauth_error="invalid_grant",
            )

        with self.assertRaises(AuthenticationRequired):
            self._ensure(bad)
        self.assertIsNone(self.store.load("fake_oauth"))

    def test_revoked_deletes_credentials(self):
        def bad(config, **kwargs):
            raise TokenRefreshFailed(
                "revoked",
                provider_id="fake_oauth",
                kind="permanent",
                retryable=False,
                oauth_error="revoked",
            )

        with self.assertRaises(AuthenticationRequired):
            self._ensure(bad)
        self.assertIsNone(self.store.load("fake_oauth"))

    def test_timeout_retains_credentials(self):
        def bad(config, **kwargs):
            raise TokenRefreshFailed(
                "timeout",
                provider_id="fake_oauth",
                kind="transient",
                retryable=True,
            )

        with self.assertRaises(TokenRefreshFailed) as ctx:
            self._ensure(bad)
        self.assertTrue(ctx.exception.retryable)
        cred = self.store.load("fake_oauth")
        self.assertIsNotNone(cred)
        assert cred is not None
        self.assertEqual(cred.refresh_token, "rt-keep-me")
        # Must not return/use expired access token via ensure_fresh
        self.assertEqual(cred.access_token, "stale-access")

    def test_connection_error_retains(self):
        def bad(config, **kwargs):
            raise TokenRefreshFailed(
                "conn",
                provider_id="fake_oauth",
                kind="transient",
                retryable=True,
            )

        with self.assertRaises(TokenRefreshFailed):
            self._ensure(bad)
        self.assertIsNotNone(self.store.load("fake_oauth"))

    def test_http_429_retains(self):
        def bad(config, **kwargs):
            raise TokenRefreshFailed(
                "rate",
                provider_id="fake_oauth",
                kind="transient",
                retryable=True,
                status_code=429,
            )

        with self.assertRaises(TokenRefreshFailed):
            self._ensure(bad)
        self.assertIsNotNone(self.store.load("fake_oauth"))

    def test_http_500_retains(self):
        def bad(config, **kwargs):
            raise TokenRefreshFailed(
                "server",
                provider_id="fake_oauth",
                kind="transient",
                retryable=True,
                status_code=500,
            )

        with self.assertRaises(TokenRefreshFailed):
            self._ensure(bad)
        self.assertIsNotNone(self.store.load("fake_oauth"))

    def test_missing_configuration_retains(self):
        bad_cfg = OAuthProviderConfig(
            provider_id="fake_oauth",
            authorize_url="http://127.0.0.1/authorize",
            token_url="",
            client_id="",
        )
        with self.assertRaises(ProviderNotConfigured):
            ensure_fresh_credential(
                self.store, bad_cfg, skew_seconds=120, now=1000.0
            )
        self.assertIsNotNone(self.store.load("fake_oauth"))

    def test_malformed_local_credential(self):
        class _BoomStore:
            def load(self, provider_id):
                raise ValueError("credential payload must be an object")

            def delete(self, provider_id):
                return True

            def save(self, provider_id, credential):
                raise AssertionError("should not save")

        with self.assertRaises(AuthenticationRequired):
            ensure_fresh_credential(
                _BoomStore(), self.config, skew_seconds=120, now=1000.0
            )

    def test_successful_refresh_updates(self):
        def ok(config, **kwargs):
            return TokenResponse(
                access_token="new-access",
                refresh_token="new-rt",
                expires_in=3600,
            )

        fresh = self._ensure(ok)
        self.assertEqual(fresh.access_token, "new-access")
        self.assertEqual(self.store.load("fake_oauth").refresh_token, "new-rt")

    def test_should_delete_only_permanent(self):
        self.assertTrue(
            should_delete_credentials(
                TokenRefreshFailed("x", kind="permanent", retryable=False)
            )
        )
        self.assertFalse(
            should_delete_credentials(
                TokenRefreshFailed("x", kind="transient", retryable=True)
            )
        )

    def test_refresh_access_token_messages_sanitized(self):
        # Simulate HTTP 400 invalid_grant via _post_form carrier.
        from auth.oauth_client import _TokenEndpointHTTPError, TokenExchangeFailed

        def boom(*args, **kwargs):
            raise TokenExchangeFailed("token endpoint HTTP 400") from _TokenEndpointHTTPError(
                400, "invalid_grant", {"error": "invalid_grant"}
            )

        with mock.patch("auth.oauth_client._post_form", side_effect=boom):
            with self.assertRaises(TokenRefreshFailed) as ctx:
                refresh_access_token(self.config, refresh_token="rt")
        msg = str(ctx.exception)
        self.assertNotIn("SECRET", msg)
        self.assertEqual(ctx.exception.kind, "permanent")
        self.assertEqual(ctx.exception.oauth_error, "invalid_grant")


if __name__ == "__main__":
    unittest.main()

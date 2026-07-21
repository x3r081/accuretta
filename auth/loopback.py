"""Loopback OAuth authorization-code callback listener.

Adapted from Nous Research Hermes Agent (MIT) loopback callback patterns
(Honcho oauth_flow.py bind/capture and Spotify redirect validation ideas).
See THIRD_PARTY_NOTICES.md.

Security constraints for Accuretta:
- bind only to 127.0.0.1 or ::1
- short callback timeout
- reject missing / duplicated / mismatched parameters
- compare state with hmac.compare_digest
"""

from __future__ import annotations

import hmac
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from providers.errors import (
    AuthenticationCancelled,
    OAuthStateMismatch,
    ProviderError,
)

DEFAULT_CALLBACK_TIMEOUT_SECONDS = 120.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


@dataclass
class LoopbackCallbackResult:
    code: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None
    error_description: Optional[str] = None
    raw_query: str = ""


@dataclass
class LoopbackListener:
    host: str = "127.0.0.1"
    port: int = 0  # OS-assigned
    path: str = "/callback"
    timeout_seconds: float = DEFAULT_CALLBACK_TIMEOUT_SECONDS
    expected_state: Optional[str] = None
    _server: Optional[HTTPServer] = None
    _result: LoopbackCallbackResult = field(default_factory=LoopbackCallbackResult)
    _done: threading.Event = field(default_factory=threading.Event)
    _error: Optional[BaseException] = None

    def __post_init__(self) -> None:
        if self.host not in LOOPBACK_HOSTS:
            raise ValueError(f"loopback host must be 127.0.0.1 or ::1, got {self.host!r}")
        if not self.path.startswith("/"):
            raise ValueError("callback path must start with /")

    @property
    def redirect_uri(self) -> str:
        if self._server is None:
            raise RuntimeError("listener not started")
        host = self.host
        # Bracket IPv6 for URI form.
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = self._server.server_address[1]
        return f"http://{host}:{port}{self.path}"

    def start(self) -> str:
        result_box = self._result
        path = self.path
        done = self._done
        listener = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != path:
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    listener._ingest_query(parsed.query)
                    body = (
                        b"<!doctype html><meta charset=utf-8><title>Accuretta</title>"
                        b"<body style='font:14px sans-serif;padding:2rem'>"
                        b"Authentication complete. You can close this tab and return to Accuretta."
                        b"</body>"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:
                    listener._error = exc
                    err_body = b"Authentication failed. You can close this tab."
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(err_body)
                finally:
                    done.set()

            def log_message(self, *_args):
                return

        self._server = HTTPServer((self.host, int(self.port)), _Handler)
        thread = threading.Thread(target=self._serve_until_done, daemon=True)
        thread.start()
        return self.redirect_uri

    def _serve_until_done(self) -> None:
        assert self._server is not None
        deadline = time.monotonic() + float(self.timeout_seconds)
        self._server.timeout = 0.5
        try:
            while not self._done.is_set() and time.monotonic() < deadline:
                self._server.handle_request()
            if not self._done.is_set():
                self._error = TimeoutError(
                    f"OAuth callback timed out after {self.timeout_seconds:.0f}s"
                )
                self._done.set()
        finally:
            try:
                self._server.server_close()
            except Exception:
                pass

    def _ingest_query(self, query: str) -> None:
        self._result.raw_query = query or ""
        params = parse_qs(query, keep_blank_values=True)

        def _one(name: str) -> Optional[str]:
            values = params.get(name)
            if not values:
                return None
            if len(values) != 1:
                raise ProviderError(
                    f"OAuth callback parameter {name!r} must not be duplicated",
                    provider_id=None,
                )
            return values[0]

        error = _one("error")
        code = _one("code")
        state = _one("state")
        error_description = _one("error_description")

        self._result.error = error
        self._result.error_description = error_description
        self._result.code = code
        self._result.state = state

        if error:
            raise AuthenticationCancelled(
                error_description or error or "authorization denied"
            )

        if not code:
            raise ProviderError("OAuth callback missing authorization code")

        if self.expected_state is not None:
            if state is None or not hmac.compare_digest(state, self.expected_state):
                raise OAuthStateMismatch("OAuth state mismatch")

    def wait(self) -> LoopbackCallbackResult:
        finished = self._done.wait(timeout=float(self.timeout_seconds) + 1.0)
        self.close()
        if not finished:
            raise TimeoutError("OAuth callback wait failed")
        if self._error is not None:
            raise self._error
        return self._result

    def close(self) -> None:
        if self._server is not None:
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None


def validate_loopback_redirect_uri(redirect_uri: str) -> Tuple[str, int, str]:
    """Return (host, port, path) for an http loopback redirect URI."""
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http":
        raise ValueError("loopback redirect_uri must use http")
    host = parsed.hostname or ""
    if host not in LOOPBACK_HOSTS and host != "localhost":
        raise ValueError("loopback redirect_uri host must be 127.0.0.1, ::1, or localhost")
    # Normalize localhost to 127.0.0.1 for binding decisions by callers.
    bind_host = "127.0.0.1" if host == "localhost" else host
    port = parsed.port or 80
    path = parsed.path or "/callback"
    return bind_host, int(port), path

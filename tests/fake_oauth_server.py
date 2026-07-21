"""In-process fake OAuth authorization server for Accuretta tests.

Newly written for Accuretta. Never contacts a real external provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse


class FakeOAuthServer:
    """Minimal authorize + token endpoints with PKCE verification."""

    def __init__(self, *, host: str = "127.0.0.1"):
        self.host = host
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.client_id = "accuretta-test-client"
        self.valid_codes: Dict[str, dict] = {}
        self.refresh_tokens: Dict[str, dict] = {}
        self.force_token_error: Optional[str] = None
        self.issued_access_tokens = []
        self.authorize_hits = 0
        self.token_hits = 0

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        port = self._httpd.server_address[1]
        return f"http://{self.host}:{port}"

    @property
    def authorize_url(self) -> str:
        return f"{self.base_url}/authorize"

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/token"

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != "/authorize":
                    self.send_response(404)
                    self.end_headers()
                    return
                server.authorize_hits += 1
                params = parse_qs(parsed.query)
                # Echo back to redirect_uri for browser-flow tests that drive
                # the redirect themselves; this endpoint just validates shape.
                redirect_uri = (params.get("redirect_uri") or [""])[0]
                state = (params.get("state") or [""])[0]
                challenge = (params.get("code_challenge") or [""])[0]
                client_id = (params.get("client_id") or [""])[0]
                body = json.dumps({
                    "redirect_uri": redirect_uri,
                    "state": state,
                    "code_challenge": challenge,
                    "client_id": client_id,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != "/token":
                    self.send_response(404)
                    self.end_headers()
                    return
                server.token_hits += 1
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                form = {k: (v[0] if v else "") for k, v in parse_qs(raw).items()}
                self._handle_token(form)

            def _handle_token(self, form: dict) -> None:
                if server.force_token_error:
                    self._json(400, {"error": server.force_token_error})
                    return
                grant = form.get("grant_type")
                if grant == "authorization_code":
                    code = form.get("code") or ""
                    entry = server.valid_codes.get(code)
                    if not entry:
                        self._json(400, {"error": "invalid_grant"})
                        return
                    if form.get("client_id") != server.client_id:
                        self._json(400, {"error": "invalid_client"})
                        return
                    if form.get("redirect_uri") != entry["redirect_uri"]:
                        self._json(400, {"error": "invalid_grant", "error_description": "redirect_uri"})
                        return
                    verifier = form.get("code_verifier") or ""
                    if not _pkce_matches(verifier, entry["challenge"]):
                        self._json(400, {"error": "invalid_grant", "error_description": "pkce"})
                        return
                    access = f"access-{code}"
                    refresh = f"refresh-{code}"
                    server.issued_access_tokens.append(access)
                    server.refresh_tokens[refresh] = {
                        "access_prefix": access,
                        "scopes": entry.get("scopes", "test"),
                    }
                    del server.valid_codes[code]
                    self._json(200, {
                        "access_token": access,
                        "refresh_token": refresh,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": entry.get("scopes", "test"),
                    })
                    return
                if grant == "refresh_token":
                    refresh = form.get("refresh_token") or ""
                    entry = server.refresh_tokens.get(refresh)
                    if not entry:
                        self._json(400, {"error": "invalid_grant"})
                        return
                    # Rotate refresh token
                    new_refresh = refresh + "-rotated"
                    access = entry["access_prefix"] + "-refreshed"
                    server.issued_access_tokens.append(access)
                    del server.refresh_tokens[refresh]
                    server.refresh_tokens[new_refresh] = {
                        "access_prefix": access,
                        "scopes": entry.get("scopes", "test"),
                    }
                    self._json(200, {
                        "access_token": access,
                        "refresh_token": new_refresh,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": entry.get("scopes", "test"),
                    })
                    return
                self._json(400, {"error": "unsupported_grant_type"})

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        self._httpd = HTTPServer((self.host, 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def issue_code(
        self,
        *,
        redirect_uri: str,
        challenge: str,
        scopes: str = "test",
        code: Optional[str] = None,
    ) -> str:
        value = code or f"code-{len(self.valid_codes)+1}"
        self.valid_codes[value] = {
            "redirect_uri": redirect_uri,
            "challenge": challenge,
            "scopes": scopes,
        }
        return value

    def callback_url(
        self,
        redirect_uri: str,
        *,
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
        error_description: Optional[str] = None,
        extra: Optional[Dict[str, str]] = None,
    ) -> str:
        params = {}
        if code is not None:
            params["code"] = code
        if state is not None:
            params["state"] = state
        if error is not None:
            params["error"] = error
        if error_description is not None:
            params["error_description"] = error_description
        if extra:
            params.update(extra)
        sep = "&" if "?" in redirect_uri else "?"
        return f"{redirect_uri}{sep}{urlencode(params)}"


def _pkce_matches(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    calc = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return calc == challenge

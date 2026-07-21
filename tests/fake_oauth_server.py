"""In-process fake OAuth authorization server for Accuretta tests.

Newly written for Accuretta. Never contacts a real external provider.
Supports authorization-code + PKCE and RFC 8628 device authorization.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse


class FakeOAuthServer:
    """Minimal authorize + device + token endpoints with PKCE verification."""

    def __init__(self, *, host: str = "127.0.0.1"):
        self.host = host
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.client_id = "accuretta-test-client"
        self.valid_codes: Dict[str, dict] = {}
        self.refresh_tokens: Dict[str, dict] = {}
        self.device_sessions: Dict[str, dict] = {}
        self.force_token_error: Optional[str] = None
        self.force_device_error: Optional[str] = None
        self.force_token_http_status: Optional[int] = None
        self.issued_access_tokens = []
        self.authorize_hits = 0
        self.token_hits = 0
        self.device_hits = 0
        self.device_poll_behavior: Dict[str, list] = {}
        self._lock = threading.Lock()

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

    @property
    def device_url(self) -> str:
        return f"{self.base_url}/device"

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
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                form = {k: (v[0] if v else "") for k, v in parse_qs(raw).items()}
                if parsed.path == "/device":
                    server.device_hits += 1
                    self._handle_device(form)
                    return
                if parsed.path == "/token":
                    server.token_hits += 1
                    self._handle_token(form)
                    return
                self.send_response(404)
                self.end_headers()

            def _handle_device(self, form: dict) -> None:
                if server.force_device_error:
                    self._json(400, {"error": server.force_device_error})
                    return
                if form.get("client_id") != server.client_id:
                    self._json(400, {"error": "invalid_client"})
                    return
                device_code = f"device-{secrets.token_hex(8)}"
                user_code = "ABCD-EFGH"
                with server._lock:
                    server.device_sessions[device_code] = {
                        "user_code": user_code,
                        "status": "pending",
                        "scopes": form.get("scope") or "test",
                        "created_at": time.time(),
                        "expires_in": 600,
                        "interval": 1,
                    }
                self._json(200, {
                    "device_code": device_code,
                    "user_code": user_code,
                    "verification_uri": f"{server.base_url}/device/verify",
                    "verification_uri_complete": (
                        f"{server.base_url}/device/verify?user_code={user_code}"
                    ),
                    "expires_in": 600,
                    "interval": 1,
                })

            def _handle_token(self, form: dict) -> None:
                if server.force_token_http_status:
                    status = int(server.force_token_http_status)
                    self._json(status, {"error": "server_error"})
                    return
                if server.force_token_error:
                    self._json(400, {"error": server.force_token_error})
                    return
                grant = form.get("grant_type")
                if grant == "urn:ietf:params:oauth:grant-type:device_code":
                    self._handle_device_token(form)
                    return
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

            def _handle_device_token(self, form: dict) -> None:
                if form.get("client_id") != server.client_id:
                    self._json(400, {"error": "invalid_client"})
                    return
                device_code = form.get("device_code") or ""
                with server._lock:
                    scripted = server.device_poll_behavior.get(device_code)
                    if scripted:
                        nxt = scripted.pop(0)
                        if not scripted:
                            server.device_poll_behavior.pop(device_code, None)
                        if isinstance(nxt, dict):
                            status = int(nxt.get("http_status") or 400)
                            body = {k: v for k, v in nxt.items() if k != "http_status"}
                            self._json(status, body)
                            return
                    entry = server.device_sessions.get(device_code)
                    if not entry:
                        self._json(400, {"error": "invalid_grant"})
                        return
                    status = entry.get("status") or "pending"
                    if status == "pending":
                        self._json(400, {"error": "authorization_pending"})
                        return
                    if status == "slow_down":
                        entry["status"] = "pending"
                        self._json(400, {"error": "slow_down"})
                        return
                    if status == "denied":
                        del server.device_sessions[device_code]
                        self._json(400, {"error": "access_denied"})
                        return
                    if status == "expired":
                        del server.device_sessions[device_code]
                        self._json(400, {"error": "expired_token"})
                        return
                    if status == "approved":
                        access = f"access-device-{device_code[-8:]}"
                        refresh = f"refresh-device-{device_code[-8:]}"
                        server.issued_access_tokens.append(access)
                        server.refresh_tokens[refresh] = {
                            "access_prefix": access,
                            "scopes": entry.get("scopes", "test"),
                        }
                        del server.device_sessions[device_code]
                        self._json(200, {
                            "access_token": access,
                            "refresh_token": refresh,
                            "token_type": "Bearer",
                            "expires_in": 3600,
                            "scope": entry.get("scopes", "test"),
                        })
                        return
                self._json(400, {"error": "authorization_pending"})

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

    def approve_device(self, device_code: str) -> None:
        with self._lock:
            entry = self.device_sessions.get(device_code)
            if entry is not None:
                entry["status"] = "approved"

    def deny_device(self, device_code: str) -> None:
        with self._lock:
            entry = self.device_sessions.get(device_code)
            if entry is not None:
                entry["status"] = "denied"

    def expire_device(self, device_code: str) -> None:
        with self._lock:
            entry = self.device_sessions.get(device_code)
            if entry is not None:
                entry["status"] = "expired"

    def mark_device_slow_down(self, device_code: str) -> None:
        with self._lock:
            entry = self.device_sessions.get(device_code)
            if entry is not None:
                entry["status"] = "slow_down"

    def latest_device_code(self) -> Optional[str]:
        with self._lock:
            if not self.device_sessions:
                return None
            return list(self.device_sessions.keys())[-1]

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

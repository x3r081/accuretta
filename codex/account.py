"""Codex account operations (JSON-RPC wrappers).

Newly written for Accuretta. Never handles tokens — Codex owns persistence.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

from .protocol import (
    PendingLogin,
    SafeAccountView,
    parse_account_read_result,
    parse_account_updated,
    parse_login_completed,
    parse_login_start_result,
    sanitize_error_message,
)
from .rpc_client import CodexRpcClient, CodexRpcError


class CodexAccountError(Exception):
    pass


class CodexAccountController:
    def __init__(self, rpc: CodexRpcClient):
        self._rpc = rpc
        self._pending: Optional[PendingLogin] = None
        self._lock = threading.RLock()
        self._account = SafeAccountView()
        self._generation = 0

    @property
    def pending(self) -> Optional[PendingLogin]:
        with self._lock:
            return self._pending

    @property
    def account(self) -> SafeAccountView:
        with self._lock:
            return self._account

    def clear_pending(self) -> None:
        with self._lock:
            self._pending = None

    def account_read(self, *, refresh_token: bool = False) -> SafeAccountView:
        try:
            result = self._rpc.request(
                "account/read",
                {"refreshToken": bool(refresh_token)},
                timeout=30.0,
            )
        except CodexRpcError as exc:
            raise CodexAccountError(sanitize_error_message(str(exc))) from None
        view = parse_account_read_result(result)
        with self._lock:
            self._account = view
        return view

    def start_browser_login(self) -> PendingLogin:
        with self._lock:
            self._generation += 1
            gen = self._generation
        try:
            result = self._rpc.request(
                "account/login/start",
                {"type": "chatgpt"},
                timeout=30.0,
            )
            pending = parse_login_start_result(result, expected_type="chatgpt")
        except (CodexRpcError, ValueError) as exc:
            raise CodexAccountError(sanitize_error_message(str(exc))) from None
        pending.started_at = time.time()
        with self._lock:
            if gen != self._generation:
                raise CodexAccountError("Login superseded")
            self._pending = pending
        return pending

    def start_device_login(self) -> PendingLogin:
        with self._lock:
            self._generation += 1
            gen = self._generation
        try:
            result = self._rpc.request(
                "account/login/start",
                {"type": "chatgptDeviceCode"},
                timeout=30.0,
            )
            pending = parse_login_start_result(result, expected_type="chatgptDeviceCode")
        except (CodexRpcError, ValueError) as exc:
            raise CodexAccountError(sanitize_error_message(str(exc))) from None
        pending.started_at = time.time()
        with self._lock:
            if gen != self._generation:
                raise CodexAccountError("Login superseded")
            self._pending = pending
        return pending

    def cancel_login(self, login_id: Optional[str] = None) -> dict:
        with self._lock:
            pending = self._pending
            if pending is None:
                return {"ok": True, "cancelled": False, "reason": "no_pending_login"}
            target = (login_id or pending.login_id or "").strip()
            if target != pending.login_id:
                return {"ok": False, "cancelled": False, "reason": "stale_login_id"}
            active_id = pending.login_id
        try:
            self._rpc.request(
                "account/login/cancel",
                {"loginId": active_id},
                timeout=15.0,
            )
        except CodexRpcError as exc:
            raise CodexAccountError(sanitize_error_message(str(exc))) from None
        with self._lock:
            if self._pending is not None and self._pending.login_id == active_id:
                self._pending = None
        return {"ok": True, "cancelled": True, "loginId": active_id}

    def logout(self) -> SafeAccountView:
        try:
            self._rpc.request("account/logout", {}, timeout=30.0)
        except CodexRpcError as exc:
            raise CodexAccountError(sanitize_error_message(str(exc))) from None
        with self._lock:
            self._pending = None
        return self.account_read(refresh_token=False)

    def handle_notification(self, method: str, params: Any) -> None:
        if method == "account/login/completed":
            parsed = parse_login_completed(params)
            login_id = parsed.get("loginId")
            with self._lock:
                pending = self._pending
                if pending is None or not login_id or pending.login_id != login_id:
                    return
                if parsed.get("success"):
                    pending.status = "completed"
                    pending.auth_url = None
                    pending.verification_url = None
                    pending.user_code = None
                    pending.error = None
                else:
                    pending.status = "failed"
                    pending.error = parsed.get("error") or "Login failed"
                    pending.auth_url = None
                    pending.verification_url = None
                    pending.user_code = None
            try:
                self.account_read(refresh_token=False)
            except Exception:
                pass
            return
        if method == "account/updated":
            upd = parse_account_updated(params)
            with self._lock:
                if upd.get("authMode") is not None:
                    self._account.auth_mode = upd.get("authMode")
                    self._account.authenticated = bool(upd.get("authMode"))
                if upd.get("planType") is not None:
                    self._account.plan_type = upd.get("planType")
            try:
                self.account_read(refresh_token=False)
            except Exception:
                pass

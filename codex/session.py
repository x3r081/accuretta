"""High-level Codex session: discovery + process + account.

Newly written for Accuretta. Does not touch AuthStore or Codex credential files.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional

from .account import CodexAccountController, CodexAccountError
from .discover import CodexDiscovery, discover_codex, reset_discovery_cache
from .flags import is_codex_inference_enabled
from .process import CodexAppServerProcess, CodexProcessError
from .protocol import sanitize_error_message
from .rpc_client import CodexRpcClient
from .status import build_codex_status_dto

log = logging.getLogger("accuretta.codex.session")

NotificationListener = Callable[[str, object], None]
ServerRequestHandler = Callable[[dict], None]

_SESSION: Optional["CodexSession"] = None
_SESSION_LOCK = threading.Lock()


def get_codex_session() -> "CodexSession":
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = CodexSession()
        return _SESSION


def reset_codex_session() -> None:
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.shutdown()
        _SESSION = None


def shutdown_codex() -> None:
    reset_codex_session()


class CodexSession:
    def __init__(self, *, executable: Optional[str] = None, process_factory=None):
        self._lock = threading.RLock()
        self._discovery: Optional[CodexDiscovery] = None
        self._process: Optional[CodexAppServerProcess] = None
        self._account: Optional[CodexAccountController] = None
        self._last_error: Optional[str] = None
        self._explicit_executable = executable
        self._process_factory = process_factory
        self._login_pending_blocks_restart = False
        self._extra_listeners: List[NotificationListener] = []
        self._server_request_handler: Optional[ServerRequestHandler] = None

    @property
    def rpc(self) -> CodexRpcClient:
        with self._lock:
            if self._process is None or not self._process.ready:
                raise CodexProcessError("Codex app-server is not running")
            return self._process.rpc

    def add_notification_listener(self, listener: NotificationListener) -> None:
        with self._lock:
            if listener not in self._extra_listeners:
                self._extra_listeners.append(listener)

    def set_server_request_handler(self, handler: Optional[ServerRequestHandler]) -> None:
        with self._lock:
            self._server_request_handler = handler
            if self._process is not None and self._process._rpc is not None:
                self._process._rpc._on_server_request = handler

    def refresh_discovery(self) -> CodexDiscovery:
        reset_discovery_cache()
        if self._explicit_executable:
            d = discover_codex(force_refresh=True, candidates=[self._explicit_executable])
        else:
            d = discover_codex(force_refresh=True)
        with self._lock:
            self._discovery = d
        return d

    def discovery(self) -> CodexDiscovery:
        with self._lock:
            if self._discovery is None:
                pass
            else:
                return self._discovery
        return self.refresh_discovery()

    def process_state(self) -> str:
        with self._lock:
            if self._process is None:
                return "stopped"
            if self._process.ready:
                return "ready"
            return "error"

    def ensure_ready(self) -> CodexAccountController:
        with self._lock:
            if self._process is not None and self._process.ready and self._account is not None:
                return self._account
            if self._login_pending_blocks_restart and self._account and self._account.pending:
                raise CodexProcessError("Codex process unavailable during pending login")

        d = self.discovery()
        if not d.available or not d.executable:
            raise CodexProcessError(d.disabled_reason or "Codex CLI not installed")

        with self._lock:
            if self._process is not None:
                try:
                    self._process.terminate()
                except Exception:
                    pass
                self._process = None
                self._account = None

            factory = self._process_factory or CodexAppServerProcess
            proc = factory(d.executable)
            account_box: dict = {}
            listeners_box: dict = {"list": list(self._extra_listeners)}
            server_handler_box: dict = {"handler": self._server_request_handler}

            def on_notify(method, params):
                ctrl = account_box.get("ctrl")
                if ctrl is not None:
                    ctrl.handle_notification(method, params)
                for listener in list(listeners_box.get("list") or []):
                    try:
                        listener(method, params)
                    except Exception as exc:
                        log.warning(
                            "codex session listener failed: %s",
                            type(exc).__name__,
                        )

            def on_server_request(msg: dict):
                handler = server_handler_box.get("handler")
                if handler is not None:
                    try:
                        handler(msg)
                    except Exception:
                        pass

            proc.set_notification_handler(on_notify)
            try:
                proc.start()
            except Exception as exc:
                self._last_error = sanitize_error_message(str(exc))
                raise
            if proc._rpc is not None:
                proc._rpc._on_server_request = on_server_request
            ctrl = CodexAccountController(proc.rpc)
            account_box["ctrl"] = ctrl
            self._process = proc
            self._account = ctrl
            self._last_error = None
            listeners_box["list"] = self._extra_listeners
            server_handler_box["handler"] = self._server_request_handler
            try:
                ctrl.account_read(refresh_token=False)
            except Exception as exc:
                self._last_error = sanitize_error_message(str(exc))
            return ctrl

    def status_dto(self, *, live: bool = False) -> dict:
        d = self.discovery()
        account = None
        pending = None
        available = False
        error = self._last_error
        process_state = self.process_state()
        if live and d.available:
            try:
                ctrl = self.ensure_ready()
                account = ctrl.account
                pending = ctrl.pending
                available = True
                process_state = self.process_state()
                error = None
            except Exception as exc:
                error = sanitize_error_message(str(exc))
                available = False
                process_state = "error"
        elif not live:
            available = bool(d.available)
            if self._account is not None and process_state == "ready":
                account = self._account.account
                pending = self._account.pending
        elif self._account is not None and process_state == "ready":
            account = self._account.account
            pending = self._account.pending
            available = True
        return build_codex_status_dto(
            discovery=d,
            process_state=process_state,
            account=account,
            pending=pending,
            provider_available=available and d.available,
            disabled_reason=None if (available and d.available) else (error or d.disabled_reason),
            error=error,
            inference_flag_enabled=is_codex_inference_enabled(),
        )

    def start_browser_login(self) -> dict:
        ctrl = self.ensure_ready()
        with self._lock:
            self._login_pending_blocks_restart = True
        try:
            pending = ctrl.start_browser_login()
        except CodexAccountError:
            with self._lock:
                self._login_pending_blocks_restart = False
            raise
        return pending.to_safe_dict()

    def start_device_login(self) -> dict:
        ctrl = self.ensure_ready()
        with self._lock:
            self._login_pending_blocks_restart = True
        try:
            pending = ctrl.start_device_login()
        except CodexAccountError:
            with self._lock:
                self._login_pending_blocks_restart = False
            raise
        return pending.to_safe_dict()

    def login_status(self) -> dict:
        dto = self.status_dto(live=False)
        pending = self._account.pending if self._account else None
        if pending is None or pending.status != "pending":
            with self._lock:
                self._login_pending_blocks_restart = False
        return dto

    def cancel_login(self, login_id: Optional[str] = None) -> dict:
        if self._account is None:
            return {"ok": True, "cancelled": False, "reason": "no_session"}
        out = self._account.cancel_login(login_id)
        with self._lock:
            self._login_pending_blocks_restart = False
        return out

    def logout(self) -> dict:
        ctrl = self.ensure_ready()
        ctrl.logout()
        with self._lock:
            self._login_pending_blocks_restart = False
        return self.status_dto(live=True)

    def retry_process(self) -> dict:
        with self._lock:
            if self._login_pending_blocks_restart and self._account and self._account.pending:
                raise CodexProcessError("Cannot restart Codex while a login is pending")
            if self._process is not None:
                try:
                    self._process.terminate()
                except Exception:
                    pass
                self._process = None
                self._account = None
        self.ensure_ready()
        return self.status_dto(live=True)

    def shutdown(self) -> None:
        with self._lock:
            self._login_pending_blocks_restart = False
            if self._account is not None:
                try:
                    self._account.clear_pending()
                except Exception:
                    pass
            if self._process is not None:
                try:
                    self._process.terminate()
                except Exception:
                    pass
            self._process = None
            self._account = None
            self._last_error = None
            self._extra_listeners.clear()
            self._server_request_handler = None

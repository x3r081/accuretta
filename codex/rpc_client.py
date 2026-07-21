"""JSON-RPC client over newline-delimited JSON on stdio.

Newly written for Accuretta.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, Optional

from auth.redact import redact_sensitive_text

from .protocol import sanitize_error_message

log = logging.getLogger("accuretta.codex.rpc")

NotificationHandler = Callable[[str, Any], None]


class CodexRpcError(Exception):
    def __init__(self, message: str, *, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class CodexRpcClient:
    def __init__(
        self,
        *,
        write_line: Callable[[str], None],
        on_notification: Optional[NotificationHandler] = None,
        on_server_request: Optional[Callable[[dict], None]] = None,
    ):
        self._write_line = write_line
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: Dict[int, Future] = {}
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = list(self._pending.items())
            self._pending.clear()
        for _id, fut in pending:
            if not fut.done():
                fut.set_exception(CodexRpcError("Codex connection closed"))

    def request(self, method: str, params: Optional[dict] = None, *, timeout: float = 30.0) -> Any:
        fut: Future = Future()
        with self._lock:
            if self._closed:
                raise CodexRpcError("Codex connection closed")
            req_id = self._next_id
            self._next_id += 1
            self._pending[req_id] = fut
        payload = {"method": method, "id": req_id, "params": params if params is not None else {}}
        self._send(payload)
        try:
            return fut.result(timeout=timeout)
        except Exception:
            with self._lock:
                self._pending.pop(req_id, None)
            raise

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        payload = {"method": method, "params": params if params is not None else {}}
        self._send(payload)

    def respond(self, req_id: Any, result: Any) -> None:
        """Reply to a server-initiated JSON-RPC request."""
        self._send({"id": req_id, "result": result})

    def respond_error(self, req_id: Any, *, code: int, message: str) -> None:
        self._send({
            "id": req_id,
            "error": {"code": int(code), "message": sanitize_error_message(message)},
        })

    def _send(self, payload: dict) -> None:
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._write_lock:
            if self._closed:
                raise CodexRpcError("Codex connection closed")
            self._write_line(line)

    def feed_line(self, line: str) -> None:
        text = (line or "").strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            log.warning("codex rpc: malformed JSON line discarded")
            return
        if not isinstance(msg, dict):
            return
        if "id" in msg and ("result" in msg or "error" in msg):
            self._handle_response(msg)
            return
        if "method" in msg and "id" in msg:
            # Server-initiated request — Accuretta does not handle these in this milestone.
            if self._on_server_request is not None:
                try:
                    self._on_server_request(msg)
                except Exception:
                    pass
            else:
                log.info("codex rpc: ignoring server request method=%s", msg.get("method"))
            return
        if "method" in msg:
            method = str(msg.get("method") or "")
            params = msg.get("params")
            if self._on_notification is not None:
                try:
                    self._on_notification(method, params)
                except Exception as exc:
                    log.warning("codex notification handler failed: %s", type(exc).__name__)
            return

    def _handle_response(self, msg: dict) -> None:
        req_id = msg.get("id")
        try:
            req_id_int = int(req_id)
        except (TypeError, ValueError):
            return
        with self._lock:
            fut = self._pending.pop(req_id_int, None)
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg.get("error")
            if isinstance(err, dict):
                message = sanitize_error_message(err.get("message") or err.get("code") or "RPC error")
                code = err.get("code")
                try:
                    code_i = int(code) if code is not None else None
                except (TypeError, ValueError):
                    code_i = None
                fut.set_exception(CodexRpcError(message, code=code_i))
            else:
                fut.set_exception(CodexRpcError(sanitize_error_message(err)))
            return
        fut.set_result(msg.get("result"))


def redact_stderr_line(line: str) -> str:
    return redact_sensitive_text(line or "")[:500]

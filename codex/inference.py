"""Codex inference service (backend only; disabled by default).

Newly written for Accuretta. Uses the existing CodexSession / app-server
process. Speaks only documented stable methods:

  thread/start, thread/resume, turn/start, turn/interrupt

and consumes notifications:

  turn/started, item/agentMessage/delta, turn/completed, error

Never handles OAuth tokens. Not wired to the chat UI in this milestone.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

from .account import CodexAccountError
from .approvals import (
    APPROVAL_UI_TIMEOUT_S,
    approval_policy_for_write_mode,
    cache_file_change_item,
    decide_command_execution,
    decide_file_change,
    decide_permissions_request,
    get_codex_write_mode,
    map_legacy_decision,
    sandbox_for_write_mode,
)
from .flags import is_codex_inference_enabled
from .inference_types import (
    CodexInferenceAvailability,
    CodexInferenceError,
    CodexInferenceEvent,
    CodexInferenceEventType,
    CodexTurnResult,
)
from .process import CodexProcessError
from .protocol import (
    build_thread_start_params,
    build_turn_interrupt_params,
    build_turn_start_params,
    parse_agent_message_delta,
    parse_error_notification,
    parse_thread_start_result,
    parse_turn_notification,
    parse_turn_start_result,
    sanitize_error_message,
)
from .rpc_client import CodexRpcError
from .session import CodexSession, get_codex_session

log = logging.getLogger("accuretta.codex.inference")

EventCallback = Callable[[CodexInferenceEvent], None]


class CodexInferenceService:
    """Minimal inference facade over one shared CodexSession."""

    def __init__(self, session: Optional[CodexSession] = None):
        self._session = session or get_codex_session()
        # Exclusive turn execution (do not hold across notification waits).
        self._busy = threading.Lock()
        self._state_lock = threading.RLock()
        self._active_thread_id: Optional[str] = None
        self._active_turn_id: Optional[str] = None
        self._active_generation = 0
        self._cancel_requested = False
        self._event_sink: Optional[EventCallback] = None
        self._text_parts: List[str] = []
        self._terminal: Optional[CodexInferenceEvent] = None
        self._terminal_event = threading.Event()
        self._registered = False
        # Validated workspace cwd for the active/latest Codex thread (approval gate).
        self._active_workspace_cwd: Optional[str] = None
        # itemId → fileChange metadata from item/started (paths for approval).
        self._file_change_items: dict = {}
        # JSON-RPC request id → pending approval metadata (dedupe / cancel).
        self._pending_approvals: dict = {}

    # ---- availability -----------------------------------------------------

    def check_available(self, *, live: bool = True) -> CodexInferenceAvailability:
        flag = is_codex_inference_enabled()
        discovery = self._session.discovery()
        cli_ok = bool(discovery.available and discovery.executable)
        process_ready = False
        authenticated = False
        reason = None

        if not flag:
            reason = "Codex inference is disabled (ACCURETTA_CODEX_INFERENCE_ENABLED)"
        elif not cli_ok:
            reason = discovery.disabled_reason or "Codex CLI not installed"
        else:
            if live:
                try:
                    ctrl = self._session.ensure_ready()
                    process_ready = self._session.process_state() == "ready"
                    authenticated = bool(ctrl.account.authenticated)
                    if not authenticated:
                        # Refresh account state from source of truth.
                        try:
                            view = ctrl.account_read(refresh_token=False)
                            authenticated = bool(view.authenticated)
                        except Exception:
                            authenticated = False
                except CodexProcessError as exc:
                    reason = sanitize_error_message(str(exc))
                    process_ready = False
                except Exception as exc:
                    reason = sanitize_error_message(str(exc))
            else:
                process_ready = self._session.process_state() == "ready"
                if self._session._account is not None:
                    authenticated = bool(self._session._account.account.authenticated)

            if flag and cli_ok and reason is None and not authenticated:
                reason = "ChatGPT authentication required for Codex inference"
            elif flag and cli_ok and reason is None and live and not process_ready:
                reason = "Codex app-server is not ready"

        available = bool(flag and cli_ok and (not live or process_ready) and authenticated)
        if available:
            reason = None
        return CodexInferenceAvailability(
            flag_enabled=flag,
            cli_available=cli_ok,
            process_ready=process_ready if live else process_ready,
            authenticated=authenticated,
            available=available,
            reason=reason,
        )

    # ---- thread / turn ----------------------------------------------------

    def create_thread(
        self,
        *,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> str:
        self._assert_can_infer()
        ctrl = self._session.ensure_ready()
        self._ensure_notification_hook()
        if not ctrl.account.authenticated:
            try:
                ctrl.account_read(refresh_token=False)
            except Exception:
                pass
        if not ctrl.account.authenticated:
            raise CodexInferenceError(
                "ChatGPT authentication required for Codex inference",
                event_type=CodexInferenceEventType.AUTHENTICATION_REQUIRED,
            )
        mode = get_codex_write_mode()
        params = build_thread_start_params(
            model=model,
            cwd=cwd,
            sandbox=sandbox_for_write_mode(mode),
            approval_policy=approval_policy_for_write_mode(mode),
        )
        try:
            result = self._session.rpc.request("thread/start", params, timeout=60.0)
            parsed = parse_thread_start_result(result)
        except CodexRpcError as exc:
            raise CodexInferenceError(
                sanitize_error_message(str(exc)),
                event_type=CodexInferenceEventType.PROTOCOL_ERROR,
            ) from None
        except ValueError as exc:
            raise CodexInferenceError(
                sanitize_error_message(str(exc)),
                event_type=CodexInferenceEventType.PROTOCOL_ERROR,
            ) from None
        except CodexProcessError as exc:
            raise CodexInferenceError(
                sanitize_error_message(str(exc)),
                event_type=CodexInferenceEventType.PROCESS_ERROR,
            ) from None
        if isinstance(cwd, str) and cwd.strip():
            with self._state_lock:
                self._active_workspace_cwd = cwd.strip()
                self._file_change_items.clear()
        return parsed["threadId"]

    def set_active_workspace(self, cwd: Optional[str]) -> None:
        """Bind the validated workspace used for approval path checks."""
        with self._state_lock:
            if isinstance(cwd, str) and cwd.strip():
                self._active_workspace_cwd = cwd.strip()
            else:
                self._active_workspace_cwd = None

    def run_turn(
        self,
        thread_id: str,
        text: str,
        *,
        on_event: Optional[EventCallback] = None,
        timeout_s: float = 300.0,
    ) -> CodexTurnResult:
        self._assert_can_infer()
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise CodexInferenceError(
                "threadId required",
                event_type=CodexInferenceEventType.PROTOCOL_ERROR,
            )
        thread_id = thread_id.strip()

        if not self._busy.acquire(blocking=False):
            raise CodexInferenceError(
                "Another Codex turn is already in progress on this session",
                event_type=CodexInferenceEventType.UNAVAILABLE,
            )
        try:
            return self._run_turn_locked(
                thread_id, text, on_event=on_event, timeout_s=timeout_s
            )
        finally:
            self._busy.release()

    def cancel_active_turn(self) -> bool:
        """Interrupt the active turn if one is running. Safe if idle."""
        with self._state_lock:
            thread_id = self._active_thread_id
            turn_id = self._active_turn_id
            self._cancel_requested = True
        self._decline_pending_approvals(reason="cancel")
        if not thread_id or not turn_id:
            return False
        try:
            params = build_turn_interrupt_params(thread_id=thread_id, turn_id=turn_id)
            self._session.rpc.request("turn/interrupt", params, timeout=15.0)
            return True
        except Exception as exc:
            log.info(
                "codex inference: interrupt failed (%s)",
                type(exc).__name__,
            )
            return False

    # ---- internals --------------------------------------------------------

    def _assert_can_infer(self) -> None:
        if not is_codex_inference_enabled():
            raise CodexInferenceError(
                "Codex inference is disabled (ACCURETTA_CODEX_INFERENCE_ENABLED)",
                event_type=CodexInferenceEventType.UNAVAILABLE,
            )

    def _ensure_notification_hook(self) -> None:
        if self._registered:
            return
        self._session.add_notification_listener(self._on_notification)
        self._session.set_server_request_handler(self._handle_server_request)
        self._registered = True

    def _emit(self, event: CodexInferenceEvent) -> None:
        if self._event_sink is not None:
            try:
                self._event_sink(event)
            except Exception:
                pass

    def _on_notification(self, method: str, params) -> None:
        # Only handle inference methods; account notifications stay with account ctrl.
        if method not in {
            "turn/started",
            "turn/completed",
            "item/agentMessage/delta",
            "error",
            "thread/started",
            "item/started",
            "item/completed",
        }:
            return

        with self._state_lock:
            gen = self._active_generation
            active_thread = self._active_thread_id
            active_turn = self._active_turn_id
            if gen == 0 or active_thread is None:
                return

        if method == "item/agentMessage/delta":
            parsed = parse_agent_message_delta(params)
            if not parsed:
                return
            if parsed.get("threadId") and parsed["threadId"] != active_thread:
                return
            if active_turn and parsed.get("turnId") and parsed["turnId"] != active_turn:
                return  # stale turn
            delta = parsed.get("delta") or ""
            with self._state_lock:
                if self._active_generation != gen:
                    return
                self._text_parts.append(delta)
            self._emit(
                CodexInferenceEvent(
                    type=CodexInferenceEventType.TEXT_DELTA,
                    thread_id=active_thread,
                    turn_id=parsed.get("turnId") or active_turn,
                    text_delta=delta,
                )
            )
            return

        if method == "turn/started":
            parsed = parse_turn_notification(params)
            if parsed.get("threadId") and parsed["threadId"] != active_thread:
                return
            turn_id = parsed.get("turnId")
            with self._state_lock:
                if self._active_generation != gen:
                    return
                if turn_id and not self._active_turn_id:
                    self._active_turn_id = turn_id
            self._emit(
                CodexInferenceEvent(
                    type=CodexInferenceEventType.STARTED,
                    thread_id=active_thread,
                    turn_id=turn_id or active_turn,
                    status=parsed.get("status") or "inProgress",
                )
            )
            return

        if method == "turn/completed":
            parsed = parse_turn_notification(params)
            if parsed.get("threadId") and parsed["threadId"] != active_thread:
                return
            turn_id = parsed.get("turnId")
            if active_turn and turn_id and turn_id != active_turn:
                return  # stale
            status = parsed.get("status") or "completed"
            with self._state_lock:
                if self._active_generation != gen:
                    return
                cancel_requested = self._cancel_requested
                if status == "interrupted" or cancel_requested:
                    evt = CodexInferenceEvent(
                        type=CodexInferenceEventType.CANCELLED,
                        thread_id=active_thread,
                        turn_id=turn_id or active_turn,
                        status="interrupted",
                        message="Turn cancelled",
                    )
                elif status == "failed":
                    evt = CodexInferenceEvent(
                        type=CodexInferenceEventType.PROTOCOL_ERROR,
                        thread_id=active_thread,
                        turn_id=turn_id or active_turn,
                        status="failed",
                        message=parsed.get("errorMessage") or "Turn failed",
                    )
                else:
                    evt = CodexInferenceEvent(
                        type=CodexInferenceEventType.COMPLETED,
                        thread_id=active_thread,
                        turn_id=turn_id or active_turn,
                        status=status,
                    )
                self._terminal = evt
                self._terminal_event.set()
            self._emit(evt)
            return

        if method == "error":
            parsed = parse_error_notification(params)
            if parsed.get("threadId") and parsed["threadId"] != active_thread:
                return
            if active_turn and parsed.get("turnId") and parsed["turnId"] != active_turn:
                return
            if parsed.get("willRetry"):
                self._emit(
                    CodexInferenceEvent(
                        type=CodexInferenceEventType.STATUS,
                        thread_id=active_thread,
                        turn_id=parsed.get("turnId") or active_turn,
                        message=parsed.get("message") or "retrying",
                        status="retrying",
                    )
                )
                return
            evt = CodexInferenceEvent(
                type=CodexInferenceEventType.PROTOCOL_ERROR,
                thread_id=active_thread,
                turn_id=parsed.get("turnId") or active_turn,
                message=parsed.get("message") or "Codex turn error",
                status="failed",
            )
            with self._state_lock:
                if self._active_generation != gen:
                    return
                self._terminal = evt
                self._terminal_event.set()
            self._emit(evt)
            return

        if method in {"item/started", "item/completed", "thread/started"}:
            if method in {"item/started", "item/completed"} and isinstance(params, dict):
                item = params.get("item")
                cached = cache_file_change_item(item)
                if cached:
                    item_id, entry = cached
                    with self._state_lock:
                        self._file_change_items[item_id] = entry
            self._emit(
                CodexInferenceEvent(
                    type=CodexInferenceEventType.STATUS,
                    thread_id=active_thread,
                    turn_id=active_turn,
                    status=method,
                    message=method,
                )
            )

    def _handle_server_request(self, msg: dict) -> None:
        """Gate Codex native file/shell approvals per ``codex_write_mode``.

        Runs on a worker thread (session dispatches off the stdout reader) so
        Accuretta's blocking approval UI cannot stall Codex I/O.

        Codex CLI 0.144.6 emits ``item/fileChange/requestApproval`` with
        numeric JSON-RPC ids (often starting at 0). Response shape for v2:
        ``{"decision": "accept"|"decline"|"cancel"|"acceptForSession"}``.
        """
        req_id = msg.get("id")
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            rpc = self._session.rpc
        except Exception:
            return

        # Never participate in external token refresh — Accuretta does not own tokens.
        if method == "account/chatgptAuthTokens/refresh":
            try:
                rpc.respond_error(
                    req_id,
                    code=-32000,
                    message="Accuretta does not supply ChatGPT auth tokens",
                )
            except Exception:
                pass
            return

        # Deduplicate: only one pending approval per JSON-RPC request id.
        with self._state_lock:
            if req_id in self._pending_approvals:
                log.info(
                    "codex approval: duplicate request id=%s method=%s ignored",
                    req_id,
                    method,
                )
                return
            workspace_cwd = self._active_workspace_cwd
            item_cache = dict(self._file_change_items)
            active_thread = self._active_thread_id
            active_turn = self._active_turn_id
            self._pending_approvals[req_id] = {
                "method": method,
                "threadId": params.get("threadId") or active_thread,
                "turnId": params.get("turnId") or active_turn,
                "itemId": params.get("itemId"),
                "started": time.time(),
            }

        # Safe protocol diagnostic (no paths/content/tokens).
        log.info(
            "codex approval: request id=%s method=%s thread=%s turn=%s item=%s",
            req_id,
            method,
            params.get("threadId") or active_thread,
            params.get("turnId") or active_turn,
            params.get("itemId"),
        )

        mode = get_codex_write_mode()
        trust_writes = False
        try:
            import bridge
            trust_writes = bool(bridge.get_settings().get("auto_approve_write"))
        except Exception:
            trust_writes = False

        def _request_approval(title, command, details=None, timeout_s=APPROVAL_UI_TIMEOUT_S):
            import bridge
            self._emit(
                CodexInferenceEvent(
                    type=CodexInferenceEventType.STATUS,
                    thread_id=active_thread,
                    turn_id=active_turn,
                    status="waiting_for_approval",
                    message="Codex is waiting for file-write approval.",
                )
            )
            return bridge.request_approval(title, command, details, timeout_s)

        decision: dict = {"decision": "decline"}
        try:
            if method in {
                "item/fileChange/requestApproval",
                "applyPatchApproval",
            }:
                decision = decide_file_change(
                    mode=mode,
                    params=params,
                    workspace_cwd=workspace_cwd,
                    item_cache=item_cache,
                    request_approval=_request_approval,
                    timeout_s=APPROVAL_UI_TIMEOUT_S,
                    trust_writes=trust_writes,
                )
                if method == "applyPatchApproval":
                    decision = map_legacy_decision(decision)
            elif method in {
                "item/commandExecution/requestApproval",
                "execCommandApproval",
            }:
                decision = decide_command_execution(
                    mode=mode,
                    params=params,
                    workspace_cwd=workspace_cwd,
                    request_approval=_request_approval,
                    timeout_s=APPROVAL_UI_TIMEOUT_S,
                )
                if method == "execCommandApproval":
                    decision = map_legacy_decision(decision)
            elif method == "item/permissions/requestApproval":
                decision = decide_permissions_request(
                    mode=mode,
                    params=params,
                    workspace_cwd=workspace_cwd,
                )
            else:
                try:
                    rpc.respond_error(
                        req_id,
                        code=-32601,
                        message="Server request not supported by Accuretta",
                    )
                except Exception:
                    pass
                with self._state_lock:
                    self._pending_approvals.pop(req_id, None)
                return
        except Exception as exc:
            log.info(
                "codex approval: handler failed id=%s (%s)",
                req_id,
                type(exc).__name__,
            )
            decision = {"decision": "denied"} if method in {
                "applyPatchApproval", "execCommandApproval",
            } else {"decision": "decline"}
        finally:
            with self._state_lock:
                self._pending_approvals.pop(req_id, None)

        # Stale: turn/thread moved on — do not apply a late decision.
        with self._state_lock:
            cur_thread = self._active_thread_id
            cur_turn = self._active_turn_id
        req_thread = params.get("threadId")
        req_turn = params.get("turnId")
        if req_thread and cur_thread and req_thread != cur_thread:
            log.info("codex approval: stale thread for id=%s — declining", req_id)
            decision = {"decision": "decline"} if method not in {
                "applyPatchApproval", "execCommandApproval",
            } else {"decision": "denied"}
        elif req_turn and cur_turn and req_turn != cur_turn:
            log.info("codex approval: stale turn for id=%s — declining", req_id)
            decision = {"decision": "decline"} if method not in {
                "applyPatchApproval", "execCommandApproval",
            } else {"decision": "denied"}

        try:
            rpc.respond(req_id, decision)
            log.info(
                "codex approval: responded id=%s decision=%s",
                req_id,
                (decision or {}).get("decision"),
            )
        except Exception as exc:
            log.info(
                "codex approval: respond failed id=%s (%s)",
                req_id,
                type(exc).__name__,
            )

    def _decline_pending_approvals(self, *, reason: str = "") -> None:
        """Fail-closed: decline any open Codex approval RPCs (cancel/timeout/exit)."""
        with self._state_lock:
            pending = dict(self._pending_approvals)
            self._pending_approvals.clear()
        if not pending:
            return
        try:
            rpc = self._session.rpc
        except Exception:
            return
        for req_id, meta in pending.items():
            method = str((meta or {}).get("method") or "")
            if method in {"applyPatchApproval", "execCommandApproval"}:
                body = {"decision": "denied"}
            else:
                body = {"decision": "decline"}
            try:
                rpc.respond(req_id, body)
                log.info(
                    "codex approval: force-decline id=%s reason=%s",
                    req_id,
                    reason or "cleanup",
                )
            except Exception:
                pass
    def _run_turn_locked(
        self,
        thread_id: str,
        text: str,
        *,
        on_event: Optional[EventCallback],
        timeout_s: float,
    ) -> CodexTurnResult:
        ctrl = self._session.ensure_ready()
        self._ensure_notification_hook()
        if not ctrl.account.authenticated:
            try:
                ctrl.account_read(refresh_token=False)
            except Exception:
                pass
        if not ctrl.account.authenticated:
            raise CodexInferenceError(
                "ChatGPT authentication required for Codex inference",
                event_type=CodexInferenceEventType.AUTHENTICATION_REQUIRED,
            )

        events: List[CodexInferenceEvent] = []

        def _collect(evt: CodexInferenceEvent) -> None:
            events.append(evt)
            if on_event is not None:
                on_event(evt)

        self._event_sink = _collect
        self._text_parts = []
        self._terminal = None
        self._terminal_event.clear()
        with self._state_lock:
            self._cancel_requested = False
            self._active_thread_id = thread_id
            self._active_turn_id = None
            self._active_generation += 1
            generation = self._active_generation

        try:
            params = build_turn_start_params(thread_id=thread_id, text=text)
            try:
                result = self._session.rpc.request("turn/start", params, timeout=60.0)
                started = parse_turn_start_result(result)
            except CodexRpcError as exc:
                raise CodexInferenceError(
                    sanitize_error_message(str(exc)),
                    event_type=CodexInferenceEventType.PROTOCOL_ERROR,
                ) from None
            except ValueError as exc:
                raise CodexInferenceError(
                    sanitize_error_message(str(exc)),
                    event_type=CodexInferenceEventType.PROTOCOL_ERROR,
                ) from None

            turn_id = started["turnId"]
            with self._state_lock:
                self._active_turn_id = turn_id
            start_evt = CodexInferenceEvent(
                type=CodexInferenceEventType.STARTED,
                thread_id=thread_id,
                turn_id=turn_id,
                status=started.get("status") or "inProgress",
            )
            _collect(start_evt)

            # Wait for terminal notification (do not hold busy/state locks).
            deadline = time.time() + max(1.0, float(timeout_s))
            while time.time() < deadline:
                if self._session.process_state() != "ready":
                    self._decline_pending_approvals(reason="process_exit")
                    raise CodexInferenceError(
                        "Codex app-server process failed during turn",
                        event_type=CodexInferenceEventType.PROCESS_ERROR,
                    )
                if self._terminal_event.wait(timeout=0.2):
                    break
            else:
                try:
                    self.cancel_active_turn()
                except Exception:
                    pass
                self._decline_pending_approvals(reason="turn_timeout")
                raise CodexInferenceError(
                    "Codex turn timed out while waiting for approval or a reply",
                    event_type=CodexInferenceEventType.PROTOCOL_ERROR,
                )

            terminal = self._terminal
            with self._state_lock:
                text_out = "".join(self._text_parts)
            if terminal is None:
                return CodexTurnResult(
                    ok=False,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    text=text_out,
                    status="failed",
                    error_type=CodexInferenceEventType.PROTOCOL_ERROR.value,
                    error_message="Turn ended without completion notification",
                    events=list(events),
                )
            if terminal.type == CodexInferenceEventType.COMPLETED:
                return CodexTurnResult(
                    ok=True,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    text=text_out,
                    status="completed",
                    events=list(events),
                )
            if terminal.type == CodexInferenceEventType.CANCELLED:
                return CodexTurnResult(
                    ok=False,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    text=text_out,
                    status="cancelled",
                    error_type=CodexInferenceEventType.CANCELLED.value,
                    error_message=terminal.message or "Turn cancelled",
                    events=list(events),
                )
            return CodexTurnResult(
                ok=False,
                thread_id=thread_id,
                turn_id=turn_id,
                text=text_out,
                status="failed",
                error_type=terminal.type.value,
                error_message=terminal.message or "Turn failed",
                events=list(events),
            )
        except CodexInferenceError:
            raise
        except CodexProcessError as exc:
            raise CodexInferenceError(
                sanitize_error_message(str(exc)),
                event_type=CodexInferenceEventType.PROCESS_ERROR,
            ) from None
        except CodexAccountError as exc:
            raise CodexInferenceError(
                sanitize_error_message(str(exc)),
                event_type=CodexInferenceEventType.AUTHENTICATION_REQUIRED,
            ) from None
        finally:
            with self._state_lock:
                if self._active_generation == generation:
                    self._active_thread_id = None
                    self._active_turn_id = None
                    self._event_sink = None
                    self._cancel_requested = False


def get_codex_inference_service(session: Optional[CodexSession] = None) -> CodexInferenceService:
    return CodexInferenceService(session=session or get_codex_session())

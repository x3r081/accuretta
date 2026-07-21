#!/usr/bin/env python3
"""Fake Codex app-server for Accuretta tests (stdio JSONL).

Never contacts OpenAI. Controlled via environment variables:

  FAKE_CODEX_MODE=ok|fail_init|crash_after_init|unauthenticated|authenticated|crash_on_turn
  FAKE_CODEX_SECRET_MARKER=sk-fake-SECRET-marker-do-not-leak
  FAKE_CODEX_AUTO_COMPLETE=0|1
  FAKE_CODEX_TURN_DELAY_MS=50
  FAKE_CODEX_EMIT_STALE_TURN=0|1
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = (os.environ.get("FAKE_CODEX_MODE") or "ok").strip()
    secret = os.environ.get("FAKE_CODEX_SECRET_MARKER") or "sk-fake-SECRET-marker-do-not-leak"
    turn_delay = float(os.environ.get("FAKE_CODEX_TURN_DELAY_MS") or "50") / 1000.0
    if len(sys.argv) >= 2 and sys.argv[1] == "--version":
        sys.stdout.write("fake-codex 0.0.0-test\n")
        sys.stdout.flush()
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] in {"app-server", "--help"} and sys.argv[1] == "--help":
        sys.stdout.write("fake codex app-server\n")
        return 0

    account = None
    if mode in {"authenticated", "crash_on_turn"}:
        account = {
            "type": "chatgpt",
            "email": "user@example.com",
            "planType": "plus",
            "accessToken": secret,
            "refresh_token": secret + "-refresh",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.fake." + secret,
        }
    pending_login = None
    initialized = False
    threads = {}
    active_turns = {}
    # Server-initiated JSON-RPC requests awaiting client responses.
    pending_server: dict = {}

    sys.stderr.write(f"fake-codex boot Authorization: Bearer {secret}\n")
    sys.stderr.flush()

    if len(sys.argv) < 2 or sys.argv[1] != "app-server":
        return 0

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue

        # Client response to a server-initiated request (approval decisions).
        if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
            entry = pending_server.get(msg.get("id"))
            if entry is not None:
                entry["result"] = msg.get("result")
                entry["error"] = msg.get("error")
                entry["event"].set()
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialized":
            continue

        if method == "initialize":
            if mode == "fail_init":
                _emit({"id": req_id, "error": {"code": -32000, "message": "init failed"}})
                return 1
            initialized = True
            _emit({
                "id": req_id,
                "result": {
                    "userAgent": "fake-codex/0.0.0",
                    "platformFamily": "test",
                    "platformOs": "test",
                },
            })
            if mode == "crash_after_init":
                time.sleep(0.05)
                return 2
            continue

        if not initialized:
            _emit({"id": req_id, "error": {"code": -32000, "message": "Not initialized"}})
            continue

        if method == "account/read":
            _emit({"id": req_id, "result": {"account": account, "requiresOpenaiAuth": True}})
            continue

        if method == "account/login/start":
            typ = params.get("type")
            login_id = str(uuid.uuid4())
            pending_login = {"loginId": login_id, "type": typ}
            if typ == "chatgpt":
                _emit({
                    "id": req_id,
                    "result": {
                        "type": "chatgpt",
                        "loginId": login_id,
                        "authUrl": (
                            "https://chatgpt.com/auth?redirect_uri="
                            "http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback"
                            f"&state=x&access_token={secret}"
                        ),
                        "accessToken": secret,
                    },
                })
            elif typ == "chatgptDeviceCode":
                _emit({
                    "id": req_id,
                    "result": {
                        "type": "chatgptDeviceCode",
                        "loginId": login_id,
                        "verificationUrl": "https://auth.openai.com/codex/device",
                        "userCode": "ABCD-EFGH",
                        "device_code": "MUST-NOT-SURFACE-" + secret,
                        "access_token": secret,
                    },
                })
            else:
                _emit({"id": req_id, "error": {"code": -32602, "message": "unsupported type"}})

            def _complete(lid=login_id):
                time.sleep(0.15)
                if pending_login and pending_login.get("loginId") == lid:
                    nonlocal account
                    account = {
                        "type": "chatgpt",
                        "email": "user@example.com",
                        "planType": "pro",
                        "accessToken": secret,
                    }
                    _emit({
                        "method": "account/login/completed",
                        "params": {"loginId": lid, "success": True, "error": None},
                    })
                    _emit({
                        "method": "account/updated",
                        "params": {"authMode": "chatgpt", "planType": "pro"},
                    })

            if os.environ.get("FAKE_CODEX_AUTO_COMPLETE", "1") == "1":
                threading.Thread(target=_complete, daemon=True).start()
            continue

        if method == "account/login/cancel":
            lid = params.get("loginId")
            if pending_login and pending_login.get("loginId") == lid:
                pending_login = None
            _emit({"id": req_id, "result": {}})
            _emit({
                "method": "account/login/completed",
                "params": {"loginId": lid, "success": False, "error": "cancelled"},
            })
            continue

        if method == "account/logout":
            account = None
            pending_login = None
            _emit({"id": req_id, "result": {}})
            _emit({
                "method": "account/updated",
                "params": {"authMode": None, "planType": None},
            })
            continue

        if method == "thread/start":
            if account is None:
                _emit({"id": req_id, "error": {"code": -32001, "message": "Unauthorized"}})
                continue
            thread_id = str(uuid.uuid4())
            model = params.get("model") or "fake-model"
            cwd = params.get("cwd") or "/tmp"
            thread = {
                "id": thread_id,
                "sessionId": thread_id,
                "preview": "",
                "modelProvider": "openai",
                "createdAt": int(time.time()),
                "updatedAt": int(time.time()),
                "status": {"type": "idle"},
                "cwd": cwd,
                "cliVersion": "0.0.0-test",
                "source": "appServer",
                "ephemeral": False,
                "turns": [],
            }
            threads[thread_id] = thread
            _emit({
                "id": req_id,
                "result": {
                    "thread": thread,
                    "model": model,
                    "modelProvider": "openai",
                    "cwd": cwd,
                    "approvalPolicy": params.get("approvalPolicy") or "on-request",
                    "approvalsReviewer": "user",
                    "sandbox": {"type": "readOnly"},
                    "accessToken": secret,
                },
            })
            _emit({"method": "thread/started", "params": {"thread": thread}})
            continue

        if method == "turn/start":
            if mode == "crash_on_turn":
                _emit({
                    "id": req_id,
                    "result": {
                        "turn": {"id": str(uuid.uuid4()), "items": [], "status": "inProgress"},
                    },
                })
                time.sleep(0.05)
                return 3
            thread_id = params.get("threadId")
            if thread_id not in threads:
                _emit({"id": req_id, "error": {"code": -32602, "message": "unknown thread"}})
                continue
            if account is None:
                _emit({"id": req_id, "error": {"code": -32001, "message": "Unauthorized"}})
                continue
            turn_id = str(uuid.uuid4())
            turn = {"id": turn_id, "items": [], "status": "inProgress"}
            active_turns[turn_id] = {"threadId": thread_id, "cancelled": False}
            _emit({"id": req_id, "result": {"turn": turn}})
            _emit({"method": "turn/started", "params": {"threadId": thread_id, "turn": turn}})

            def _stream(tid=turn_id, thid=thread_id):
                time.sleep(turn_delay)
                if active_turns.get(tid, {}).get("cancelled"):
                    _emit({
                        "method": "turn/completed",
                        "params": {
                            "threadId": thid,
                            "turn": {"id": tid, "items": [], "status": "interrupted"},
                        },
                    })
                    return

                write_path = (os.environ.get("FAKE_CODEX_WRITE_PATH") or "").strip()
                if os.environ.get("FAKE_CODEX_REQUEST_WRITE") == "1" and write_path:
                    item_id = "file-change-" + str(uuid.uuid4())
                    _emit({
                        "method": "item/started",
                        "params": {
                            "threadId": thid,
                            "turnId": tid,
                            "item": {
                                "type": "fileChange",
                                "id": item_id,
                                "status": "inProgress",
                                "changes": [{
                                    "path": write_path,
                                    "kind": "add",
                                    "diff": "hello from approved Codex write\n",
                                }],
                            },
                        },
                    })
                    srv_id = "srv-write-" + str(uuid.uuid4())
                    ev = threading.Event()
                    pending_server[srv_id] = {"event": ev, "result": None, "error": None}
                    _emit({
                        "id": srv_id,
                        "method": "item/fileChange/requestApproval",
                        "params": {
                            "threadId": thid,
                            "turnId": tid,
                            "itemId": item_id,
                            "reason": "create demo file",
                        },
                    })
                    ev.wait(timeout=30.0)
                    entry = pending_server.pop(srv_id, {}) or {}
                    decision = None
                    if isinstance(entry.get("result"), dict):
                        decision = entry["result"].get("decision")
                    if decision == "accept":
                        try:
                            with open(write_path, "w", encoding="utf-8") as fh:
                                fh.write("hello from approved Codex write\n")
                        except Exception as exc:
                            sys.stderr.write(f"fake-codex write failed: {exc}\n")
                            sys.stderr.flush()
                        status_fc = "completed"
                        text = f"Created {write_path}"
                    else:
                        status_fc = "declined"
                        text = "write access was declined again."
                    _emit({
                        "method": "item/completed",
                        "params": {
                            "threadId": thid,
                            "turnId": tid,
                            "item": {
                                "type": "fileChange",
                                "id": item_id,
                                "status": status_fc,
                                "changes": [{
                                    "path": write_path,
                                    "kind": "add",
                                    "diff": "hello from approved Codex write\n",
                                }],
                            },
                        },
                    })
                    msg_id = str(uuid.uuid4())
                    _emit({
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": thid,
                            "turnId": tid,
                            "itemId": msg_id,
                            "delta": text,
                        },
                    })
                    _emit({
                        "method": "turn/completed",
                        "params": {
                            "threadId": thid,
                            "turn": {"id": tid, "items": [], "status": "completed"},
                        },
                    })
                    return

                item_id = str(uuid.uuid4())
                _emit({
                    "method": "item/started",
                    "params": {
                        "threadId": thid,
                        "turnId": tid,
                        "startedAtMs": int(time.time() * 1000),
                        "item": {"type": "agentMessage", "id": item_id, "text": ""},
                    },
                })
                for piece in ("Hello", " from", " Codex"):
                    if active_turns.get(tid, {}).get("cancelled"):
                        break
                    time.sleep(max(turn_delay / 3, 0.01))
                    _emit({
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": thid,
                            "turnId": tid,
                            "itemId": item_id,
                            "delta": piece,
                            "accessToken": secret,
                        },
                    })
                if os.environ.get("FAKE_CODEX_EMIT_STALE_TURN") == "1":
                    stale = str(uuid.uuid4())
                    _emit({
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": thid,
                            "turnId": stale,
                            "itemId": "stale-item",
                            "delta": " STALE",
                        },
                    })
                    _emit({
                        "method": "turn/completed",
                        "params": {
                            "threadId": thid,
                            "turn": {"id": stale, "items": [], "status": "completed"},
                        },
                    })
                if active_turns.get(tid, {}).get("cancelled"):
                    status = "interrupted"
                else:
                    status = "completed"
                    _emit({
                        "method": "item/completed",
                        "params": {
                            "threadId": thid,
                            "turnId": tid,
                            "completedAtMs": int(time.time() * 1000),
                            "item": {
                                "type": "agentMessage",
                                "id": item_id,
                                "text": "Hello from Codex",
                            },
                        },
                    })
                _emit({"method": "item/agentMessage/delta", "params": "not-a-dict"})
                _emit({
                    "method": "turn/completed",
                    "params": {
                        "threadId": thid,
                        "turn": {"id": tid, "items": [], "status": status},
                    },
                })

            threading.Thread(target=_stream, daemon=True).start()
            continue

        if method == "turn/interrupt":
            turn_id = params.get("turnId")
            thread_id = params.get("threadId")
            if turn_id in active_turns:
                active_turns[turn_id]["cancelled"] = True
            _emit({"id": req_id, "result": {}})
            if turn_id and thread_id:
                _emit({
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": turn_id, "items": [], "status": "interrupted"},
                    },
                })
            continue

        _emit({"id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

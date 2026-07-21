#!/usr/bin/env python3
"""Fake Codex app-server for Accuretta tests (stdio JSONL).

Never contacts OpenAI. Controlled via environment variables:

  FAKE_CODEX_MODE=ok|fail_init|crash_after_init|unauthenticated|authenticated
  FAKE_CODEX_SECRET_MARKER=sk-fake-SECRET-marker-do-not-leak
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
    # Version probe path — exit before entering RPC loop.
    if len(sys.argv) >= 2 and sys.argv[1] == "--version":
        sys.stdout.write("fake-codex 0.0.0-test\n")
        sys.stdout.flush()
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] in {"app-server", "--help"} and sys.argv[1] == "--help":
        sys.stdout.write("fake codex app-server\n")
        return 0

    account = None
    if mode == "authenticated":
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

    # Optional stderr poison (must be redacted by Accuretta readers)
    sys.stderr.write(f"fake-codex boot Authorization: Bearer {secret}\n")
    sys.stderr.flush()

    if len(sys.argv) < 2 or sys.argv[1] != "app-server":
        # Ignore unknown modes; only app-server speaks JSONL.
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
            # Never required — but include poison in raw for parser tests when authenticated
            result = {"account": account, "requiresOpenaiAuth": True}
            _emit({"id": req_id, "result": result})
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
            # Auto-complete shortly unless cancelled.
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

        _emit({"id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

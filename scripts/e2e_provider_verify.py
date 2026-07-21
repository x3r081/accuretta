#!/usr/bin/env python3
"""E2E provider routing + UI accuracy verification against a live Accuretta bridge."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("ACCURETTA_E2E_BASE", "http://127.0.0.1:52180")
LOG = Path(os.environ.get("ACCURETTA_E2E_LOG", "/tmp/accuretta-e2e/bridge.log"))
OUT = Path(os.environ.get("ACCURETTA_E2E_OUT", "/tmp/accuretta-e2e/results.json"))
ROOT = Path(__file__).resolve().parent.parent if (Path(__file__).name != "e2e_provider_verify.py") else Path(".")


def api(method: str, path: str, body=None, timeout=180):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" in ctype or path == "/api/chat":
                return {"_sse": raw, "_status": resp.status}
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"raw": raw}
        raise RuntimeError(f"{method} {path} -> {e.code}: {payload}") from None


def parse_sse(raw: str) -> list[dict]:
    events = []
    for block in raw.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.startswith("data:")]
        if not lines:
            continue
        payload = "".join(ln[5:].lstrip() for ln in lines)
        if not payload or payload.strip() == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except Exception:
            continue
    return events


def select_provider(pid: str) -> dict:
    return api("POST", f"/api/providers/{pid}/select", {})


def create_chat(title: str) -> dict:
    return api("POST", "/api/chats", {"title": title, "origin": "desktop"})


def get_chats() -> dict:
    return api("GET", "/api/chats")


def get_settings() -> dict:
    return api("GET", "/api/settings")


def chat_turn(chat_id: str, message: str, timeout=180) -> list[dict]:
    # Stream chat; collect SSE events
    data = json.dumps({
        "chat_id": chat_id,
        "message": message,
        "mode": "agent",
    }).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return parse_sse(raw)


def log_slice(start_size: int) -> str:
    if not LOG.exists():
        return ""
    data = LOG.read_bytes()
    return data[start_size:].decode("utf-8", errors="replace")


def log_size() -> int:
    return LOG.stat().st_size if LOG.exists() else 0


def find_dispatch(log_text: str) -> list[str]:
    return re.findall(r"(?:\[provider\]\s*)?chat_dispatch[^\n]*", log_text)


def assert_safe_text(blob: str, label: str, findings: list):
    lowered = blob.lower()
    bad_patterns = [
        (r"authorization\s*:", "Authorization header"),
        (r"bearer\s+[a-z0-9\-._~+/]+=*", "Bearer token"),
        (r"\"access_token\"\s*:", "access_token field"),
        (r"\"refresh_token\"\s*:", "refresh_token field"),
        (r"sk-[a-zA-Z0-9]{20,}", "sk- style secret"),
    ]
    for pat, name in bad_patterns:
        if re.search(pat, blob, re.I):
            findings.append(f"LEAK in {label}: {name}")


def main():
    results = {"scenarios": {}, "findings": [], "ok": True}
    findings = results["findings"]

    health = api("GET", "/api/health")
    assert health.get("app") == "accuretta", health
    results["health"] = health

    providers = api("GET", "/api/providers")
    results["providers_selected"] = providers.get("selectedProviderId")
    codex = next((p for p in providers.get("providers") or [] if p.get("providerId") == "codex_chatgpt"), None)
    results["codex_selectable"] = bool(codex and codex.get("selectable"))

    # ---------- 1-3 Local ----------
    sc = {}
    select_provider("local_llama")
    settings = get_settings()
    sc["settings_provider"] = settings.get("provider_id")
    chat_local = create_chat("e2e-local")
    local_id = chat_local["id"]
    sc["chat"] = {
        "id": local_id,
        "inference_provider_id": chat_local.get("inference_provider_id"),
        "inference_provider_label": chat_local.get("inference_provider_label"),
    }
    # UI header/composer derive from these fields
    sc["header_would_show"] = chat_local.get("inference_provider_label") or chat_local.get("inference_provider_id")
    sc["composer_mode"] = "local" if chat_local.get("inference_provider_id") == "local_llama" else "unexpected"
    sc["local_model"] = settings.get("model")

    before = log_size()
    events = chat_turn(local_id, "Reply with exactly: LOCAL_CONNECTION_OK")
    after_log = log_slice(before)
    dispatches = find_dispatch(after_log)
    sc["dispatch_lines"] = dispatches
    finals = [e for e in events if e.get("type") == "final"]
    providers_ev = [e for e in events if e.get("type") == "provider"]
    sc["provider_events"] = providers_ev
    final_msg = (finals[-1].get("message") if finals else {}) or {}
    sc["response_meta"] = {
        "provider_id": final_msg.get("provider_id"),
        "provider_label": final_msg.get("provider_label"),
        "model_label": final_msg.get("model_label"),
        "content_snip": (final_msg.get("content") or "")[:120],
    }
    # Persist check
    chats = get_chats()
    persisted = (chats.get("chats") or {}).get(local_id) or {}
    sc["persisted_provider"] = persisted.get("inference_provider_id")
    last_as = None
    for m in reversed(persisted.get("messages") or []):
        if m.get("role") == "assistant":
            last_as = m
            break
    sc["persisted_message_meta"] = {
        "provider_id": (last_as or {}).get("provider_id"),
        "provider_label": (last_as or {}).get("provider_label"),
    }

    ok_local = (
        sc["settings_provider"] == "local_llama"
        and sc["chat"]["inference_provider_id"] == "local_llama"
        and "Local" in (sc["header_would_show"] or "")
        and any("dispatched=local_llama" in d for d in dispatches)
        and sc["response_meta"].get("provider_id") == "local_llama"
        and "Local" in (sc["response_meta"].get("provider_label") or "")
    )
    sc["pass"] = ok_local
    if not ok_local:
        findings.append(f"Local scenario failed: {json.dumps(sc, indent=2)[:1500]}")
    results["scenarios"]["1-3_local"] = sc

    # ---------- 4-8 Codex ----------
    sc2 = {}
    select_provider("codex_chatgpt")
    settings = get_settings()
    sc2["settings_provider"] = settings.get("provider_id")
    chat_codex = create_chat("e2e-codex")
    codex_id = chat_codex["id"]
    sc2["chat"] = {
        "id": codex_id,
        "inference_provider_id": chat_codex.get("inference_provider_id"),
        "inference_provider_label": chat_codex.get("inference_provider_label"),
        "codex_thread_id": chat_codex.get("codex_thread_id"),
    }
    sc2["header_would_show"] = chat_codex.get("inference_provider_label")
    sc2["composer_mode"] = "codex" if chat_codex.get("inference_provider_id") == "codex_chatgpt" else "unexpected"
    sc2["composer_must_not_show_qwen_as_active"] = chat_codex.get("inference_provider_id") != "local_llama"

    before = log_size()
    # Instrument: wrap is hard live; detect llama completion by log markers / absence of local dispatch
    events1 = chat_turn(codex_id, "Reply with exactly: CODEX_CONNECTION_OK")
    log1 = log_slice(before)
    disp1 = find_dispatch(log1)
    sc2["dispatch_turn1"] = disp1
    sc2["llama_completion_in_log"] = bool(re.search(r"/v1/chat/completions|llama_post_stream|dispatched=local_llama", log1))
    # More precise: dispatch must be codex only
    sc2["dispatched_codex"] = any("dispatched=codex_chatgpt" in d for d in disp1)
    sc2["dispatched_local"] = any("dispatched=local_llama" in d for d in disp1)

    finals1 = [e for e in events1 if e.get("type") == "final"]
    msg1 = (finals1[-1].get("message") if finals1 else {}) or {}
    sc2["response_meta_turn1"] = {
        "provider_id": msg1.get("provider_id"),
        "provider_label": msg1.get("provider_label"),
        "model_label": msg1.get("model_label"),
        "content_snip": (msg1.get("content") or "")[:200],
    }
    err1 = [e for e in events1 if e.get("type") == "error"]
    sc2["errors_turn1"] = err1

    chats = get_chats()
    persisted_c = (chats.get("chats") or {}).get(codex_id) or {}
    sc2["thread_after_turn1"] = persisted_c.get("codex_thread_id")
    sc2["persisted_provider"] = persisted_c.get("inference_provider_id")

    before2 = log_size()
    events2 = chat_turn(codex_id, "Reply with exactly: CODEX_THREAD_OK")
    log2 = log_slice(before2)
    disp2 = find_dispatch(log2)
    sc2["dispatch_turn2"] = disp2
    finals2 = [e for e in events2 if e.get("type") == "final"]
    msg2 = (finals2[-1].get("message") if finals2 else {}) or {}
    sc2["response_meta_turn2"] = {
        "provider_id": msg2.get("provider_id"),
        "provider_label": msg2.get("provider_label"),
        "content_snip": (msg2.get("content") or "")[:200],
    }
    chats = get_chats()
    persisted_c2 = (chats.get("chats") or {}).get(codex_id) or {}
    sc2["thread_after_turn2"] = persisted_c2.get("codex_thread_id")
    sc2["thread_continuity"] = (
        bool(sc2["thread_after_turn1"])
        and sc2["thread_after_turn1"] == sc2["thread_after_turn2"]
    )

    ok_codex = (
        sc2["settings_provider"] == "codex_chatgpt"
        and sc2["chat"]["inference_provider_id"] == "codex_chatgpt"
        and "Codex" in (sc2["header_would_show"] or "")
        and sc2["composer_must_not_show_qwen_as_active"]
        and sc2["dispatched_codex"]
        and not sc2["dispatched_local"]
        and sc2["response_meta_turn1"].get("provider_id") == "codex_chatgpt"
        and "Codex" in (sc2["response_meta_turn1"].get("provider_label") or "")
        and sc2["response_meta_turn2"].get("provider_id") == "codex_chatgpt"
        and sc2["thread_continuity"]
        and not err1
    )
    sc2["pass"] = ok_codex
    if not ok_codex:
        findings.append(f"Codex scenario failed: {json.dumps(sc2, indent=2)[:2500]}")
    results["scenarios"]["4-8_codex"] = sc2

    # ---------- 9-10 Settings switch while Codex session open ----------
    sc3 = {}
    select_provider("local_llama")
    settings = get_settings()
    sc3["settings_now"] = settings.get("provider_id")
    chats = get_chats()
    still = (chats.get("chats") or {}).get(codex_id) or {}
    sc3["open_session_provider"] = still.get("inference_provider_id")
    sc3["open_session_label"] = still.get("inference_provider_label")
    sc3["still_has_codex_thread"] = bool(still.get("codex_thread_id"))
    # Send another message on Codex session — must still dispatch Codex
    before = log_size()
    events3 = chat_turn(codex_id, "Reply with exactly: STILL_CODEX")
    log3 = log_slice(before)
    disp3 = find_dispatch(log3)
    sc3["dispatch_while_settings_local"] = disp3
    finals3 = [e for e in events3 if e.get("type") == "final"]
    msg3 = (finals3[-1].get("message") if finals3 else {}) or {}
    sc3["response_meta"] = {
        "provider_id": msg3.get("provider_id"),
        "provider_label": msg3.get("provider_label"),
        "content_snip": (msg3.get("content") or "")[:120],
    }
    notices = [e for e in events3 if e.get("type") == "notice" and e.get("code") == "provider_session_mismatch"]
    sc3["mismatch_notice"] = notices[0].get("note") if notices else None
    ok_mismatch = (
        sc3["settings_now"] == "local_llama"
        and sc3["open_session_provider"] == "codex_chatgpt"
        and any("dispatched=codex_chatgpt" in d for d in disp3)
        and not any("dispatched=local_llama" in d for d in disp3)
        and sc3["response_meta"].get("provider_id") == "codex_chatgpt"
        and sc3["mismatch_notice"]
        and "Codex" in (sc3["mismatch_notice"] or "")
        and "Local" in (sc3["mismatch_notice"] or "")
    )
    sc3["pass"] = ok_mismatch
    if not ok_mismatch:
        findings.append(f"Mismatch scenario failed: {json.dumps(sc3, indent=2)[:2000]}")
    results["scenarios"]["9-10_mismatch"] = sc3

    # ---------- 11 New session uses local ----------
    sc4 = {}
    chat_new = create_chat("e2e-local-after-switch")
    sc4["chat"] = {
        "id": chat_new["id"],
        "inference_provider_id": chat_new.get("inference_provider_id"),
        "inference_provider_label": chat_new.get("inference_provider_label"),
    }
    before = log_size()
    events4 = chat_turn(chat_new["id"], "Reply with exactly: NEW_LOCAL_OK")
    log4 = log_slice(before)
    disp4 = find_dispatch(log4)
    sc4["dispatch"] = disp4
    finals4 = [e for e in events4 if e.get("type") == "final"]
    msg4 = (finals4[-1].get("message") if finals4 else {}) or {}
    sc4["response_meta"] = {
        "provider_id": msg4.get("provider_id"),
        "provider_label": msg4.get("provider_label"),
    }
    ok_new = (
        sc4["chat"]["inference_provider_id"] == "local_llama"
        and any("dispatched=local_llama" in d for d in disp4)
        and sc4["response_meta"].get("provider_id") == "local_llama"
    )
    sc4["pass"] = ok_new
    if not ok_new:
        findings.append(f"New local session failed: {json.dumps(sc4, indent=2)[:1500]}")
    results["scenarios"]["11_new_local"] = sc4

    # Snapshot IDs for restart check
    results["persist_ids"] = {
        "local_id": local_id,
        "codex_id": codex_id,
        "new_local_id": chat_new["id"],
        "codex_thread": sc2.get("thread_after_turn2"),
    }

    # Safety scan of logs produced during this run
    full_log = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
    assert_safe_text(full_log, "bridge.log", findings)
    chats_blob = json.dumps(get_chats())
    settings_blob = json.dumps(get_settings())
    assert_safe_text(chats_blob, "chats.json api", findings)
    assert_safe_text(settings_blob, "settings.json api", findings)

    results["ok"] = all(s.get("pass") for s in results["scenarios"].values()) and not findings
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"ok": results["ok"], "out": str(OUT), "findings": findings}, indent=2))
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    # Allow running from repo root without installing
    sys.exit(main())

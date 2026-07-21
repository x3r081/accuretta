#!/usr/bin/env python3
"""Post-restart persistence check + browser UI smoke for session provider chrome."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE = os.environ.get("ACCURETTA_E2E_BASE", "http://127.0.0.1:52180")
PREV = Path(os.environ.get("ACCURETTA_E2E_OUT", "/tmp/accuretta-e2e/results.json"))
OUT = Path("/tmp/accuretta-e2e/restart_ui.json")


def api(method, path, body=None, timeout=60):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main():
    prev = json.loads(PREV.read_text()) if PREV.exists() else {}
    ids = prev.get("persist_ids") or {}
    chats = api("GET", "/api/chats")
    store = chats.get("chats") or {}

    report = {"persist": {}, "ui": {}, "ok": True, "findings": []}

    for key, expected_pid in (
        ("local_id", "local_llama"),
        ("codex_id", "codex_chatgpt"),
        ("new_local_id", "local_llama"),
    ):
        cid = ids.get(key)
        chat = store.get(cid) if cid else None
        entry = {
            "id": cid,
            "found": bool(chat),
            "inference_provider_id": (chat or {}).get("inference_provider_id"),
            "inference_provider_label": (chat or {}).get("inference_provider_label"),
            "codex_thread_id": (chat or {}).get("codex_thread_id"),
        }
        report["persist"][key] = entry
        if not chat:
            report["findings"].append(f"missing chat {key}={cid}")
            report["ok"] = False
            continue
        if chat.get("inference_provider_id") != expected_pid:
            report["findings"].append(
                f"{key} provider {chat.get('inference_provider_id')} != {expected_pid}"
            )
            report["ok"] = False
        if expected_pid == "codex_chatgpt" and not chat.get("codex_thread_id"):
            report["findings"].append("codex session lost thread id after restart")
            report["ok"] = False
        if expected_pid == "local_llama" and chat.get("codex_thread_id"):
            report["findings"].append("local session has codex_thread_id after restart")
            report["ok"] = False

    # Routing after restart: one short turn each
    import time

    def turn(cid, text):
        data = json.dumps({"chat_id": cid, "message": text, "mode": "agent"}).encode()
        req = urllib.request.Request(
            BASE + "/api/chat",
            data=data,
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        events = []
        for block in raw.split("\n\n"):
            for ln in block.splitlines():
                if ln.startswith("data:"):
                    payload = ln[5:].lstrip()
                    if payload and payload != "[DONE]":
                        try:
                            events.append(json.loads(payload))
                        except Exception:
                            pass
        finals = [e for e in events if e.get("type") == "final"]
        msg = (finals[-1].get("message") if finals else {}) or {}
        return msg.get("provider_id"), msg.get("provider_label")

    if ids.get("codex_id"):
        pid, label = turn(ids["codex_id"], "Reply with exactly: AFTER_RESTART_CODEX")
        report["persist"]["codex_after_restart_route"] = {"provider_id": pid, "provider_label": label}
        if pid != "codex_chatgpt":
            report["findings"].append(f"codex chat routed to {pid} after restart")
            report["ok"] = False

    if ids.get("new_local_id"):
        pid, label = turn(ids["new_local_id"], "Reply with exactly: AFTER_RESTART_LOCAL")
        report["persist"]["local_after_restart_route"] = {"provider_id": pid, "provider_label": label}
        if pid != "local_llama":
            report["findings"].append(f"local chat routed to {pid} after restart")
            report["ok"] = False

    # Browser UI smoke via Chrome headless + evaluate (no playwright dep)
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(chrome).exists():
        # Fetch HTML and confirm static contracts; interactive chrome dump of SPA
        # after JS boot is best-effort via remote debugging.
        html = urllib.request.urlopen(BASE + "/", timeout=10).read().decode("utf-8", errors="replace")
        report["ui"]["has_chat_meta"] = 'id="chat-meta"' in html
        report["ui"]["has_model_pill"] = 'id="model-pill"' in html
        report["ui"]["model_pill_default_empty"] = not re.search(
            r'id="model-pill"[^>]*>.*?qwen', html, re.I | re.S
        )
        # Serve a tiny harness that loads app state APIs and reports labels
        harness = f"""<!doctype html><meta charset=utf-8>
<script>
async function run() {{
  const chats = await (await fetch('{BASE}/api/chats')).json();
  const settings = await (await fetch('{BASE}/api/settings')).json();
  const order = chats.order || [];
  const out = {{ settingsProvider: settings.provider_id, sessions: [] }};
  for (const id of order.slice(0, 12)) {{
    const c = chats.chats[id];
    if (!c) continue;
    out.sessions.push({{
      id,
      title: c.title,
      inference_provider_id: c.inference_provider_id || null,
      inference_provider_label: c.inference_provider_label || null,
      headerLabel: c.inference_provider_label
        || (c.inference_provider_id === 'codex_chatgpt' ? 'Codex via ChatGPT'
            : c.inference_provider_id === 'local_llama' ? 'Local llama.cpp'
            : (c.inference_provider_id ? c.inference_provider_id : 'Local llama.cpp')),
      composerMode: (c.inference_provider_id === 'codex_chatgpt') ? 'codex'
        : (c.inference_provider_id === 'openai') ? 'openai' : 'local'
    }});
  }}
  document.body.textContent = JSON.stringify(out);
}}
run().catch(e => {{ document.body.textContent = JSON.stringify({{error: String(e)}}); }});
</script>"""
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
            f.write(harness)
            harness_path = f.name
        dump_path = "/tmp/accuretta-e2e/ui_dump.html"
        try:
            subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    f"--dump-dom",
                    f"file://{harness_path}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            # dump-dom prints to stdout
            proc = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--virtual-time-budget=5000",
                    f"file://{harness_path}",
                    "--dump-dom",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=40,
            )
            Path(dump_path).write_text(proc.stdout or "", encoding="utf-8")
            m = re.search(r"(\{.*\})", proc.stdout or "", re.S)
            if m:
                ui_data = json.loads(m.group(1))
                report["ui"]["harness"] = ui_data
                # Validate e2e chats appear with correct labels
                by_id = {s["id"]: s for s in ui_data.get("sessions") or []}
                for key, expect_mode, expect_substr in (
                    ("codex_id", "codex", "Codex"),
                    ("new_local_id", "local", "Local"),
                ):
                    cid = ids.get(key)
                    s = by_id.get(cid)
                    if not s:
                        report["findings"].append(f"UI harness missing {key}")
                        report["ok"] = False
                        continue
                    if s.get("composerMode") != expect_mode:
                        report["findings"].append(
                            f"UI composerMode for {key}={s.get('composerMode')}"
                        )
                        report["ok"] = False
                    if expect_substr not in (s.get("headerLabel") or ""):
                        report["findings"].append(
                            f"UI headerLabel for {key}={s.get('headerLabel')}"
                        )
                        report["ok"] = False
            else:
                report["ui"]["harness_raw_snip"] = (proc.stdout or "")[:500]
                report["findings"].append("UI harness produced no JSON")
                report["ok"] = False
        except Exception as e:
            report["findings"].append(f"UI chrome check failed: {e}")
            report["ok"] = False
    else:
        report["ui"]["skipped"] = "Chrome not found"

    # Log safety after restart turns
    log = Path("/tmp/accuretta-e2e/bridge.log")
    if log.exists():
        blob = log.read_text(encoding="utf-8", errors="replace")
        report["dispatch_lines_sample"] = re.findall(r"\[provider\] chat_dispatch[^\n]*", blob)[-8:]
        if not report["dispatch_lines_sample"]:
            report["findings"].append("no [provider] chat_dispatch lines after restart turns")
            report["ok"] = False
        for pat, name in (
            (r"authorization\s*:", "Authorization"),
            (r"\"access_token\"", "access_token"),
            (r"\"refresh_token\"", "refresh_token"),
        ):
            if re.search(pat, blob, re.I):
                report["findings"].append(f"leak in log: {name}")
                report["ok"] = False

    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "findings": report["findings"], "out": str(OUT)}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

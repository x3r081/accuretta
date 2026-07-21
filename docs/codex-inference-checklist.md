# Codex inference — operator / developer checklist

Concise release and local-dev checklist for **Codex via ChatGPT**. Details:
[providers.md](providers.md), [codex-workspace-security.md](codex-workspace-security.md),
[codex-chatgpt-auth-smoke-test.md](codex-chatgpt-auth-smoke-test.md).

**Tested Codex CLI:** `0.144.6`

## Prerequisites

- [ ] Python 3.10+; repo deps installed as usual
- [ ] Official `codex` CLI installed; `codex --version` recorded
- [ ] Local llama.cpp still available (default provider)
- [ ] Inference flag only for intentional runs: `ACCURETTA_CODEX_INFERENCE_ENABLED=1`

## Launch (local)

```bash
ACCURETTA_PORT=8787 \
ACCURETTA_CODEX_INFERENCE_ENABLED=1 \
ACCURETTA_BROWSER=none \
python3 bridge.py
```

Optional: `ACCURETTA_CODEX_BIN=/absolute/path/to/codex`

Health identity (launcher uses this — not bare TCP):

```bash
curl -sS http://127.0.0.1:8787/api/health | python3 -m json.tool
# expect: "ok": true, "app": "accuretta"
```

## Auth vs inference

- [ ] Sign-in works with flag **off** (account status only)
- [ ] With flag **off**, Codex is **not** selectable for chat
- [ ] With flag **on** + signed in + CLI ready → Codex selectable
- [ ] Disconnect → Codex not selectable; local still works; **no** silent provider switch

## Chat / streaming / cancel

- [ ] Select **Codex via ChatGPT**; new conversation
- [ ] Simple reply streams; bubble shows **Codex via ChatGPT**
- [ ] Second turn reuses thread (no duplicate history)
- [ ] Stop mid-reply cancels (`/api/cancel` / UI Stop)
- [ ] Failure does **not** open local llama for that turn

## Workspace / security

- [ ] Thread `cwd` is Accuretta workspace (not `$HOME` / `/`)
- [ ] Native Codex file/shell approvals follow `codex_write_mode` (chat_only declines; ask prompts; workspace_auto allows in-workspace writes only)
- [ ] No tokens in `data/`, settings, chats, or bridge logs
- [ ] Accuretta shutdown ends **owned** app-server only (not unrelated `codex`)

## Ports

- [ ] Occupied non-Accuretta listener on `ACCURETTA_PORT` → launcher shows port-busy, does **not** attach
- [ ] Second Accuretta instance uses single-instance lock / already-running UI
- [ ] Override: `ACCURETTA_PORT=<free>`

## Packaging notes (desktop)

- [ ] Frozen launcher extends PATH with Homebrew bins when possible
- [ ] Prefer `ACCURETTA_CODEX_BIN` for Dock/Finder launches if `which codex` fails
- [ ] Static assets resolve from bundle root (`sys._MEIPASS` chdir)
- [ ] Do **not** claim a notarized macOS `.app` ship without a separate packaging verification pass

## Automated

```bash
python3 -m unittest discover -s tests -q
```

Focus areas: `tests/test_codex_*`, `tests/test_launcher_readiness.py`

## Troubleshoot

| Symptom | Check |
|---|---|
| CLI missing | Install Codex; set `ACCURETTA_CODEX_BIN` |
| Not selectable | Flag on? Signed in? `GET …/codex_chatgpt/status` |
| Unsupported model | Clear any accidental local GGUF id; leave `codex_model` empty |
| Port busy | Other process on port; set `ACCURETTA_PORT` |
| Orphan app-server after Force Quit | Kill only the Accuretta-spawned PID; do not blanket-kill all Codex |
| Sign-in expired | Settings → Sign in again; then re-select Codex if needed |

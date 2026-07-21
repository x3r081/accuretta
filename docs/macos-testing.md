# macOS compatibility test plan

Test plan for Accuretta on macOS. Application behavior is unchanged by this document.

**Defaults under test:** bridge on `0.0.0.0:8787` (LAN/Tailscale-reachable); managed `llama-server` on `127.0.0.1:8080`.

**Existing automated coverage (baseline):**  
`tests/test_apple_silicon_hw.py`, `tests/test_llama_discovery.py`, `tests/test_browse_folder.py`, `tests/test_ui_copy.py`

```bash
python3 -m unittest discover -s tests -v
```

---

## 1. Test matrix

### Hardware / memory

| ID | Hardware | Memory | Priority | Layer |
|----|----------|--------|----------|-------|
| HW-M1-16 | Apple Silicon M1 | 16 GB | P0 | Manual AS |
| HW-M1-32 | Apple Silicon M1/M1 Pro/Max | 32 GB | P1 | Manual AS |
| HW-M2-16 | Apple Silicon M2 | 16 GB | P0 | Manual AS |
| HW-M2-24 | Apple Silicon M2 Pro/etc. | 24 GB | P0 | Manual AS |
| HW-M3-18 | Apple Silicon M3 | ~18–36 GB class | P1 | Manual AS |
| HW-M4-24 | Apple Silicon M4 Pro | 24 GB | P0 | Manual AS |
| HW-M4-48 | Apple Silicon M4 Pro/Max | 48 GB | P1 | Manual AS |
| HW-M4-64 | Apple Silicon M4 Max/Ultra | 64+ GB | P1 | Manual AS |
| HW-INTEL | Intel Mac (x86_64) | any | P2 | Manual (where practical) |

**Per-host checks (all HW-\* rows):** chip label ≠ “Generic CPU” on Apple Silicon; usable unified memory = total − reserve; Metal hardware/build status plausible; auto-tune suggests conservative ctx (often 8K–16K on 16–24 GB); UI copy is macOS (POSIX paths, Homebrew, no WSL).

### llama-server discovery / config

| ID | Scenario | Expect | Layer |
|----|----------|--------|-------|
| LS-BREW | Homebrew `llama-server` (`/opt/homebrew` or `/usr/local`) | Auto-detect; symlink path kept; version probe OK | Manual AS + unit |
| LS-MANUAL | `llama_bin` / `ACCURETTA_LLAMA_BIN` set | Configured path never silently replaced | Unit + Manual AS |
| LS-MISSING | No binary on PATH / settings | Setup missing state; Homebrew/Metal guidance; no Windows `.exe` / WSL copy | Unit (ui_copy) + Manual AS |
| LS-METAL | Metal-capable Homebrew build | `--list-devices` shows MTL\*; recommendations mention Metal | Mocked + Manual AS |

### Paths (spaces & Unicode)

| ID | Scenario | Expect | Layer |
|----|----------|--------|-------|
| PATH-GGUF-SPACE | Model under `…/My Models/qwen 14b.gguf` | Load + chat OK | Integration + Manual AS |
| PATH-GGUF-UNI | Model under `…/模型/テスト.gguf` | Load + chat OK | Manual AS |
| PATH-WS-SPACE | Workspace `…/My Projects/app one` | Add folder, read/write tools OK | Integration + Manual AS |
| PATH-WS-UNI | Workspace with Unicode / combining marks | List/read/write OK; no mojibake in UI | Manual AS |

### Folder picker

| ID | Scenario | Expect | Layer |
|----|----------|--------|-------|
| PICK-OK | Choose folder via native picker | `{path, cancelled:false}`; path applied | Unit (mocked osascript) + Manual AS |
| PICK-CANCEL | Cancel dialog | `{cancelled:true}`; no crash | Unit + Manual AS |
| PICK-HEADLESS | `ACCURETTA_NO_GUI=1` | Structured unavailable; toast/paste path | Unit + Manual AS |

### Process / network lifecycle

| ID | Scenario | Expect | Layer |
|----|----------|--------|-------|
| NET-PORT-BUSY | Something already on llama port (8080) | Start refused with clear error; no second bind fight | Integration + Manual AS |
| NET-CRASH-GEN | Kill `llama-server` mid-stream | Stream ends or errors; watchdog restart or stuck notice; UI not infinite-spin forever\* | Integration + Manual AS |
| NET-BRIDGE-RESTART | Quit/relaunch bridge | Restores settings; reuses or respawns llama per boot rules | Manual AS + smoke |
| NET-MODEL-SWAP | Load model A → B | Old process stopped; B ready; chat uses B | Integration + Manual AS |
| NET-STREAM-LONG | Multi-minute / long completion | Deltas continue; cancel works; tok/s eventually appears | Manual AS |
| NET-SIGINT | Ctrl+C bridge | Watchdog stops; child terminated; ports freed | Manual AS + smoke |
| NET-STALE | Orphan `llama-server` after `kill -9` bridge | Next start: “port in use” or “using existing”; recoverable | Manual AS |
| NET-LOCAL | llama `--host 127.0.0.1` | Not reachable from LAN IP on 8080 | Manual AS |
| NET-LAN | Bridge `0.0.0.0:8787` | Phone/LAN browser reaches UI | Manual AS + smoke |
| NET-TAILSCALE | Tailscale IP:8787 | Remote device reaches UI; llama stays local | Manual AS |

\*Today stream idle may hang a long time (`timeout=None`); record actual behavior until timeouts land.

### Setup / persistence

| ID | Scenario | Expect | Layer |
|----|----------|--------|-------|
| SETUP-FIRST | Empty/cleared settings | Wizard; macOS copy; detect Homebrew; no WSL step | Manual AS + smoke |
| SETUP-REPEAT | Saved `models_dir` + `model_path` | No forced wizard; auto-spawn last model when possible | Manual AS + smoke |

---

## 2. Automated unit tests

Run on any host (CI-friendly). Prefer pure functions + mocks; no real GUI/Metal.

| Area | Cases | Today |
|------|-------|-------|
| Apple Silicon HW | sysctl bands 16/24/32/48/64 GB; reserve table; never Generic CPU; Metal parse | `test_apple_silicon_hw` |
| Discovery | Homebrew prefixes; PATH; env pin; missing; basename `llama-server` | `test_llama_discovery` |
| Folder picker | Darwin osascript success/cancel; no Tk; headless; spaces | `test_browse_folder` |
| UI copy | macOS catalog; no WSL/`.exe`/elevated PowerShell; POSIX placeholders | `test_ui_copy` |

**Gaps to add later (still unit/mocked):** port-busy start refusal; wait_for_llama against injected base URL; lifecycle log ring; ui feature `sandbox_wsl=false`.

---

## 3. Automated integration tests

Real subprocesses where safe; skip if no `llama-server` / no tiny GGUF.

| Case | Outline |
|------|---------|
| Fake llama-server | Controllable stub: delayed `/v1/models`, crash mid-SSE, port hold (`test_macos_acceptance`) |
| Load → ping → short chat | **Opt-in** real GGUF smoke — see §3a |
| Port occupied | Fake occupant on free port → start refused (`test_macos_acceptance`) |
| Headless picker | `ACCURETTA_NO_GUI=1` structured error (`test_browse_folder`) |

Default `unittest discover` stays green without a model: the real-GGUF smoke is skipped unless explicitly enabled.

### 3a. Opt-in macOS llama.cpp smoke (real GGUF)

**Not run by default.** Skipped unless all of: macOS, arm64, discoverable `llama-server`, and `ACCURETTA_TEST_GGUF` pointing at an existing absolute `.gguf` path.

```bash
# From repo root on Apple Silicon, with Homebrew llama.cpp installed:
export ACCURETTA_TEST_GGUF=/absolute/path/to/model.gguf
# optional pin:
# export ACCURETTA_LLAMA_BIN=/opt/homebrew/bin/llama-server
# optional load/chat deadline (seconds, default 180):
# export ACCURETTA_TEST_LLAMA_TIMEOUT=180

python3 -m unittest tests.test_macos_llama_smoke -v
```

What it does: starts `llama-server` on a free localhost port (ctx 4096, parallel 1, ngl=99 when Metal build is detected), waits for readiness, runs a minimal completion + bridge `run_chat_turn` consume, prints redacted diagnostics (version, backend/Metal evidence, startup/prompt/gen timings, peak RSS), stops the child, asserts no leftover process. Does **not** assert a minimum tok/s. Does **not** print the GGUF path.

Leave `ACCURETTA_TEST_GGUF` unset for normal CI / day-to-day:

```bash
python3 -m unittest discover -s tests -v
```

---

## 4. Mocked platform tests

Force `platform_name` / `machine` / sysctl / `list_devices` (pattern already in `test_apple_silicon_hw`).

| Mock profile | Assert |
|--------------|--------|
| `darwin` + `arm64` + M1/M2/M4 brand strings | Chip, UMA, Metal flags, recommendations |
| `darwin` + `x86_64` (Intel) | No false Apple Silicon; CPU/Metal guidance sane |
| Memory 16 / 24 / 32 / 48 / 64 GiB | Reserve + usable + ctx band |
| MTL present vs CPU-only `list-devices` | `metal_llama_build` true/false |
| `ui_copy` os=`macos` vs `windows`/`linux` | Feature gates + placeholders |

---

## 5. Manual Apple Silicon tests

Run on at least one **16–24 GB** and one **32 GB+** machine when possible. Prefer Homebrew Metal `llama.cpp`.

### Checklist (per machine)

1. **First run** — Clear or use fresh `data/` (backup first). Start bridge. Wizard: POSIX placeholders, Homebrew hint, **no WSL**. Detect `llama-server`.
2. **Memory UI** — Settings memory budget matches chip + usable UMA; auto-tune conservative on ≤24 GB.
3. **Homebrew path** — Leave `llama_bin` empty; confirm detect uses `/opt/homebrew/bin/llama-server` (Apple Silicon) or `/usr/local` (Intel Homebrew).
4. **Manual path** — Set `ACCURETTA_LLAMA_BIN` to a copy/symlink; restart; path preserved.
5. **Missing binary** — Rename/hide binary briefly; wizard/settings show brew/Metal guidance.
6. **GGUF spaces/Unicode** — Load model from awkward path; one short chat.
7. **Workspace spaces/Unicode** — Add folder; agent `list_dir` / `read_file`.
8. **Folder picker** — Success + cancel; with `ACCURETTA_NO_GUI=1`, paste path.
9. **Port busy** — `llama-server` already on 8080; load from UI → clear error.
10. **Crash mid-gen** — `kill -9` llama PID during stream; note UI + watchdog/logs.
11. **Bridge restart** — Quit normally; relaunch; saved model/settings return.
12. **Model switch** — A→B; confirm B answers.
13. **Long stream** — Ask for long output; cancel mid-way; confirm Stop.
14. **SIGINT** — Ctrl+C; `lsof -i :8080` / `:8787` clear.
15. **Stale process** — `kill -9` bridge only; orphan llama; relaunch; recover (stop orphan or use existing).
16. **Bind** — From another LAN device open `http://<mac-lan-ip>:8787` (UI works). Confirm `:8080` is **not** exposed on LAN.
17. **Tailscale** — `http://<tailscale-ip>:8787` from phone; chat works.
18. **Repeat startup** — Second launch skips wizard; auto-loads last GGUF when configured.

### Intel Mac (practical subset)

Discovery (`/usr/local`), folder picker, paths with spaces, port busy, SIGINT, LAN UI, first-run copy (still macOS, not WSL). Metal expectations differ (no AppleGPU UMA story).

---

## 6. Release smoke tests

Short gate before tagging a macOS build (≈20 minutes on one M-series Mac):

| # | Smoke step | Pass criteria |
|---|------------|---------------|
| 1 | `python3 -m unittest discover -s tests -v` | All OK (GGUF smoke skipped unless env set) |
| 1b | Opt-in: `ACCURETTA_TEST_GGUF=… python3 -m unittest tests.test_macos_llama_smoke -v` | Ready → chat → bridge consume → clean stop |
| 2 | Fresh launch + wizard | No Windows/WSL strings; Homebrew detect or clear missing state |
| 3 | Load one GGUF (path with a space if available) | `llama_running`; Backend log shows launch |
| 4 | One short chat | Visible reply; no multi-minute hang on “ping” |
| 5 | Folder picker once | Path applied or cancel clean |
| 6 | LAN open of `:8787` | UI loads |
| 7 | SIGINT | Clean exit; no orphan required for pass (note if orphan) |
| 8 | Second start with saved settings | Auto or one-click model ready |

---

## 7. Environment cheat sheet

```bash
# Headless / no browser / no picker
export ACCURETTA_NO_GUI=1
export ACCURETTA_BROWSER=none
export ACCURETTA_PORT=8787

# Pin llama-server
export ACCURETTA_LLAMA_BIN=/opt/homebrew/bin/llama-server
# or: export LLAMA_HOST=127.0.0.1:8080

# Opt-in real-GGUF smoke (see §3a) — leave unset for default tests
# export ACCURETTA_TEST_GGUF=/absolute/path/to/model.gguf

# Useful probes
curl -sS -m 2 http://127.0.0.1:8787/api/health
curl -sS -m 2 http://127.0.0.1:8080/v1/models
curl -sS 'http://127.0.0.1:8787/api/llama-log?tail=80'
ps -ax -o pid,%cpu,rss,command | grep -i '[l]lama-server'
lsof -nP -iTCP:8787 -sTCP:LISTEN
lsof -nP -iTCP:8080 -sTCP:LISTEN
```

---

## 8. Coverage map (plan vs layers)

| Topic | Unit | Integration | Mocked platform | Manual AS | Smoke |
|-------|:----:|:-----------:|:---------------:|:---------:|:-----:|
| M1–M4 / memory bands | partial | | ✓ | ✓ | |
| Intel Mac | | | ✓ | ✓ (P2) | |
| Homebrew / manual / missing llama | ✓ | ✓ | ✓ | ✓ | ✓ |
| GGUF/workspace spaces & Unicode | partial | ✓ | | ✓ | spaces |
| Folder picker / headless | ✓ | ✓ | ✓ | ✓ | ✓ |
| Port busy / crash / stale / SIGINT | | ✓ | | ✓ | SIGINT |
| Model switch / long stream / bridge restart | | ✓ | | ✓ | restart |
| Local vs LAN vs Tailscale | | | | ✓ | LAN |
| First-run / repeat setup | | | ✓ copy | ✓ | ✓ |

---

## 9. Sign-off template

| Field | Value |
|-------|-------|
| macOS version | |
| Chip / RAM | |
| `llama-server` path + version | |
| Unit tests | pass / fail |
| Smoke | pass / fail |
| Blockers | |
| Date / tester | |
)

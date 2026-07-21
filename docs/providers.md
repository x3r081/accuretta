# Inference providers

Accuretta routes inference through a small provider layer. **Local llama.cpp
remains the default.** This release also includes an **experimental OpenAI API
key** provider, **experimental GitHub account login** (authentication only —
not Copilot inference), and **experimental Codex via ChatGPT** (Codex-managed
ChatGPT authentication plus **optional, capability-gated Codex inference**).
No other cloud inference providers are supported.

## Architecture

| Layer | Role |
|---|---|
| Provider definition | Metadata: id, display name, auth type, capabilities |
| Auth / account | How credentials are acquired and stored (`auth/`) — Codex tokens stay in Codex |
| Runtime credentials | Short-lived access material for a request (never sent to the browser) |
| Model | Identifier belonging to a provider |
| API mode | Wire protocol (`local_llama`, `openai_chat`, `codex_app_server`, …) |
| Inference provider | Validate, list models, stream, cancel |

Package layout:

- `providers/` — definitions, registry, status DTOs, `LocalLlamaProvider`, `OpenAIProvider`, `CodexProvider`
- `providers/inference_stream.py` — narrow stream adapter used by `run_chat_turn`
- `codex/` — Codex CLI discovery, app-server process, account + inference RPC
- `auth/` — credential storage and generic OAuth primitives (not used for Codex tokens)
- `launcher_readiness.py` — Accuretta-specific HTTP health probe for the desktop launcher

## Local provider

- **id:** `local_llama`
- **auth:** none
- **default:** yes

## OpenAI API provider (experimental)

- **id:** `openai`
- **auth:** user-supplied API key (`auth_type: api_key`)
- **API mode:** OpenAI Chat Completions (`/v1/chat/completions` streaming)
- **Not** ChatGPT subscription / OAuth / Codex
- **Billing:** API usage is billed separately by OpenAI
- Selected model stored in settings as `openai_model` (never overwrites local `model` / `model_path`)

### Setup

1. Create an API key in the OpenAI dashboard.
2. Settings → Provider → OpenAI API → paste key → Connect.
3. Key is stored via AuthStore (macOS Keychain when available, else owner-only file).
4. Optionally Refresh models, pick a model, then Use provider.

### Credential validation

`POST /api/providers/openai/connect` validates with a lightweight `GET /v1/models`:

| Outcome | Behavior |
|---|---|
| Success | Key stored; `credentialValidated=true` |
| 401/403 | Key **not** stored; authentication error |
| 429 / network / 5xx | Key may be stored as unvalidated; retry later |

Status distinguishes `credentialStored` vs `credentialValidated`. Never returns the key.

### Chat routing

`run_chat_turn` still owns tools, approvals, iteration limits, and SSE events.
It opens the model stream through `open_provider_chat_stream`:

- local → existing llama-server SSE
- openai → official Chat Completions SSE (same OpenAI framing)
- Codex → app-server turn stream adapted to the same SSE framing

Tool definitions use the existing Accuretta OpenAI-compatible tool schema for
local/OpenAI. Codex turns do **not** use Accuretta tools; Codex native
file/shell approvals are declined (advisory chat-only).

Cancellation closes the active stream and uses `/api/cancel` (Codex →
`turn/interrupt` for the owned turn).

Disconnecting a cloud provider while it is selected blocks new chats for that
provider until you reconnect or select local llama — Accuretta does **not**
silently fall back.

## GitHub account authentication (experimental)

- **id:** `github`
- **auth:** OAuth 2.0 device authorization (`auth_type: oauth_device`)
- **purpose:** prove Accuretta can sign in a GitHub **account**
- **does not** enable GitHub Copilot inference, model listing, or chat
- **supports_inference:** `false`

### Configuration

Set an Accuretta-owned GitHub OAuth App client ID:

```bash
export ACCURETTA_GITHUB_CLIENT_ID="your-accuretta-oauth-app-client-id"
```

Create the OAuth App under the Accuretta GitHub organization/account. Enable
**Device Flow**. No client secret is required for the public device flow Accuretta
uses. Do **not** reuse client IDs from Hermes, VS Code, GitHub CLI, GitHub Copilot,
or OpenAI Codex.

If `ACCURETTA_GITHUB_CLIENT_ID` is unset, the GitHub provider remains visible but
unavailable; local llama and OpenAI keep working and startup does not fail.

### Scopes

| Scope | Why |
|---|---|
| `read:user` | Read profile login/id to show a safe account label after connect |

No repository, workflow, org-admin, package, email, or Copilot scopes.

### Flow

1. Settings → GitHub account → Connect GitHub
2. Bridge starts device authorization and shows `userCode` + verification URL
3. Bridge polls GitHub’s token endpoint (frontend never talks to GitHub)
4. On success, Accuretta calls `GET https://api.github.com/user` to validate
5. Stores access token + minimal metadata (`account_label`, `account_id`, scopes, validation time)

GitHub typically returns a non-expiring token without a refresh token — Accuretta
does not invent expiry or refresh behavior.

Disconnect deletes the stored GitHub credential, clears metadata, and cancels any
pending device session. OpenAI / local credentials are untouched.

### Capabilities advertised

`account_authentication`, `device_authorization` only — never `inference`,
`streaming`, `models`, or Copilot.

## ChatGPT / Codex (experimental)

- **id:** `codex_chatgpt`
- **display name:** Codex via ChatGPT
- **auth:** Codex-managed ChatGPT (`auth_type: codex_managed_chatgpt`)
- **API mode:** `codex_app_server` (stdio JSON-RPC)
- **inference:** capability-gated (`ACCURETTA_CODEX_INFERENCE_ENABLED`); Settings-selectable when ready
- **workspace:** official `thread/start` `cwd` only — see [codex-workspace-security.md](./codex-workspace-security.md)
- **tools / writes:** Codex native file/shell approvals are **declined** (advisory chat-only). Accuretta’s local approval gates are unchanged and are not bypassed.

### Authentication vs inference

| Concern | Who owns it | Accuretta behavior |
|---|---|---|
| ChatGPT sign-in / tokens / refresh | **Codex CLI** | Starts `codex app-server`; never stores tokens |
| Account label / plan in Settings | Codex → Accuretta safe DTO | Shown when signed in |
| Chat completions | Codex (when flag on + ready) | Select **Codex via ChatGPT** in Settings |
| Local GGUF chat | Accuretta + llama.cpp | Default provider; independent of Codex auth |

You can sign in with ChatGPT for account status **without** enabling inference.
Inference requires an explicit env flag **and** a ready Codex session.

### Prerequisites

1. Official OpenAI Codex CLI on the machine (`codex` on `PATH`, or
   `ACCURETTA_CODEX_BIN=/absolute/path/to/codex`).
2. **Tested with Codex CLI `0.144.6`.** Newer builds with the same app-server
   account + thread/turn methods are expected to work; Accuretta does not claim
   support for untested major protocol changes.
3. For inference: set `ACCURETTA_CODEX_INFERENCE_ENABLED=1` for that process
   (defaults **off**).

Discovery order:

1. `ACCURETTA_CODEX_BIN` (absolute executable)
2. `shutil.which("codex")`
3. `/opt/homebrew/bin/codex`
4. `/usr/local/bin/codex`

Packaged desktop apps (Finder/Dock) may have a minimal `PATH`; the launcher
prepends Homebrew paths when frozen. Prefer `ACCURETTA_CODEX_BIN` if discovery
fails.

### How ChatGPT sign-in works

1. Settings → ChatGPT / Codex → **Sign in with ChatGPT** (or device code).
2. Accuretta asks Codex app-server to start login; Codex opens/owns the OAuth
   browser or device flow (Accuretta does **not** host a custom OAuth callback).
3. On success, Settings shows account label / plan from a **sanitized** status DTO.
4. Disconnect calls Codex `account/logout`. Tokens remain Codex’s responsibility.

### How to enable and select Codex inference

```bash
# Local source run (example)
ACCURETTA_CODEX_INFERENCE_ENABLED=1 ACCURETTA_BROWSER=none python3 bridge.py
```

1. Confirm Settings shows ChatGPT connected and **Codex ready**.
2. Provider dropdown → **Codex via ChatGPT** → Use provider (select only when
   `selectable` / ready).
3. Start a new conversation and chat. Responses are labeled **Codex via ChatGPT**.
4. To return to local: select **Local llama.cpp** (no automatic fallback either way).

Do **not** set local `settings.model` (GGUF id) as a Codex model — Accuretta
omits local model ids when starting Codex threads (Codex uses its own default
unless `codex_model` is explicitly set).

### Protocol surface

Transport: `codex app-server` (default **stdio**, newline-delimited JSON-RPC).
No WebSocket transport.

**Auth:** `initialize` / `initialized`, `account/read`, `account/login/start`,
`account/login/completed`, `account/updated`, `account/login/cancel`,
`account/logout`.

**Inference (flag on):** `thread/start`, `turn/start`, `turn/interrupt`, plus
notifications `turn/started`, `item/agentMessage/delta`, `turn/completed`,
`error`. Native approval RPCs are answered with **decline**.

Not used: Accuretta-owned `chatgptAuthTokens`, Accuretta custom OAuth callback
servers, reading `~/.codex/auth.json`, API-key login via Codex for this provider.

### HTTP API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/providers/codex_chatgpt/status` | Safe account / process / readiness DTO |
| POST | `/api/providers/codex_chatgpt/connect` | Browser ChatGPT login start |
| POST | `/api/providers/codex_chatgpt/device/start` | Device-code login start |
| GET | `/api/providers/codex_chatgpt/login/status` | Pending / terminal login state |
| POST | `/api/providers/codex_chatgpt/login/cancel` | Cancel active loginId |
| POST | `/api/providers/codex_chatgpt/disconnect` | `account/logout` |
| POST | `/api/providers/codex_chatgpt/process/retry` | Restart **owned** app-server |
| POST | `/api/providers/codex_chatgpt/select` | Requires inference readiness |

Responses never include tokens. `authUrl` / `userCode` / `verificationUrl` are
cleared after completion, cancel, or failure and are not persisted in the browser.

### Process lifecycle

One app-server child per Accuretta runtime, started lazily for live status or
login. Accuretta shuts down **only that owned child** (and its process group on
Unix) via atexit, Ctrl+C, and `/api/shutdown`. It does **not** `pkill`/`killall`
other Codex processes on the machine. Ordinary shutdown does **not** delete Codex
credentials.

### Privacy / security model (Codex)

- No ChatGPT access/refresh tokens in AuthStore, settings, chats, or logs
- Stdio-only transport to the owned app-server
- Workspace `cwd` validated to Accuretta’s active workspace (not `$HOME` / `/`)
- No silent Codex→local or local→Codex fallback on errors
- See [codex-workspace-security.md](./codex-workspace-security.md)

### Current limitations (verified / claimed)

- Inference is **experimental** and **off by default**
- Codex turns are **advisory chat-only** (native writes/shell declined)
- No claim of support for Codex CLI versions other than those tested (`0.144.6`)
- Packaged `.app` / PyInstaller builds are not a separate verified ship artifact
  for Codex PATH beyond launcher PATH extension + `ACCURETTA_CODEX_BIN`
- Force Quit / `SIGKILL` of Accuretta may leave an orphaned owned app-server
  (same class as llama-server)

## Demonstration provider

`example_cloud` remains registered as unavailable for API/UI exercises only.

## HTTP API (safe fields only)

See earlier Phase 5 routes. OpenAI-specific:

| Method | Path | Notes |
|---|---|---|
| POST | `/api/providers/openai/connect` | Body `{ "apiKey": "..." }` |
| POST | `/api/providers/openai/disconnect` | Deletes stored key |
| GET | `/api/providers/openai/models` | Requires stored key |
| POST | `/api/providers/openai/select` | Requires stored key; optional `{ "model": "..." }` |

Generic device authorization (RFC 8628) when a provider registers a device
config factory:

| Method | Path | Notes |
|---|---|---|
| POST | `/api/providers/{id}/device/start` | Returns user code + verification URLs only |
| GET | `/api/providers/{id}/device/status` | `idle` / `pending` / `authorized` / … |
| POST | `/api/providers/{id}/device/cancel` | Cancels in-flight poller |

Never returns `deviceCode`, tokens, or client secrets.

## Frontend boundary

`app.js` may only see safe status. API key input is password-style, cleared after
submit, never written to localStorage/sessionStorage.

## Adding a future provider

1. Register a `ProviderDefinition` (+ factory).
2. Use Accuretta’s own OAuth/API-key registration — never Hermes client IDs.
3. Expose status only through `build_safe_provider_status`.
4. Plug streaming into `open_provider_chat_stream` (or extend it).
5. Keep local llama as the default.

## Related

- [authentication.md](authentication.md)
- [codex-inference-checklist.md](codex-inference-checklist.md) — operator / developer checklist
- [codex-chatgpt-auth-smoke-test.md](codex-chatgpt-auth-smoke-test.md) — ChatGPT / Codex auth checklist
- [codex-workspace-security.md](codex-workspace-security.md)
- [provider-smoke-test.md](provider-smoke-test.md) — broader provider milestone checklist
- [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)

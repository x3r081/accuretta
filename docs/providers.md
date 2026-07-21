# Inference providers

Accuretta routes inference through a small provider layer. **Local llama.cpp
remains the default.** This release also includes an **experimental OpenAI API
key** provider, **experimental GitHub account login** (authentication only —
not Copilot inference), and **experimental ChatGPT / Codex account login**
(authentication only via the official Codex app-server — not Codex inference).
No other cloud inference providers are supported.

## Architecture

| Layer | Role |
|---|---|
| Provider definition | Metadata: id, display name, auth type, capabilities |
| Auth / account | How credentials are acquired and stored (`auth/`) |
| Runtime credentials | Short-lived access material for a request (never sent to the browser) |
| Model | Identifier belonging to a provider |
| API mode | Wire protocol (`local_llama`, `openai_chat`, …) |
| Inference provider | Validate, list models, stream, cancel |

Package layout:

- `providers/` — definitions, registry, status DTOs, `LocalLlamaProvider`, `OpenAIProvider`
- `providers/inference_stream.py` — narrow stream adapter used by `run_chat_turn`
- `auth/` — credential storage and generic OAuth primitives

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

Tool definitions use the existing Accuretta OpenAI-compatible tool schema.
Cancellation closes the HTTP stream and uses the existing `/api/cancel` path.

Disconnecting OpenAI while it is selected blocks new OpenAI chats (401) until
you reconnect or select local llama — Accuretta does **not** silently fall back.

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

## ChatGPT / Codex authentication (experimental)

- **id:** `codex_chatgpt`
- **display name:** ChatGPT / Codex
- **auth:** Codex-managed ChatGPT (`auth_type: codex_managed_chatgpt`)
- **API mode:** `codex_app_server` (stdio JSON-RPC)
- **purpose:** sign in with ChatGPT through the official Codex CLI app-server
- **does not** enable Codex inference, threads, turns, tools, approvals, or model listing
- **supports_inference:** `false` (not shown in the chat provider selector)
- **selectable:** `false`

Accuretta never implements ChatGPT OAuth itself. Codex owns client registration,
browser callback / device authorization, tokens, refresh, logout, and
persistence. Accuretta never reads Codex credential files and never stores Codex
tokens in AuthStore.

### Dependency

Requires the official OpenAI Codex CLI (`codex`). Tested with **Codex CLI
0.144.6**. Discovery order:

1. `ACCURETTA_CODEX_BIN` (absolute executable)
2. `shutil.which("codex")`
3. `/opt/homebrew/bin/codex`
4. `/usr/local/bin/codex`

If Codex is missing or unsupported, the provider stays visible in account
settings with a sanitized reason; local llama and OpenAI keep working.

### Protocol (auth only)

Transport: `codex app-server` (default stdio, newline-delimited JSON-RPC).

Methods / notifications used:

- `initialize` / `initialized`
- `account/read`
- `account/login/start` (`chatgpt`, `chatgptDeviceCode`)
- `account/login/completed`, `account/updated`
- `account/login/cancel`
- `account/logout`

Not used: `chatgptAuthTokens`, API-key login via Codex, `thread/*`, `turn/*`,
`model/list`, approvals, WebSocket transport.

### HTTP API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/providers/codex_chatgpt/status` | Safe account / process DTO |
| POST | `/api/providers/codex_chatgpt/connect` | Browser ChatGPT login start |
| POST | `/api/providers/codex_chatgpt/device/start` | Device-code login start |
| GET | `/api/providers/codex_chatgpt/login/status` | Pending / terminal login state |
| POST | `/api/providers/codex_chatgpt/login/cancel` | Cancel active loginId |
| POST | `/api/providers/codex_chatgpt/disconnect` | `account/logout` |
| POST | `/api/providers/codex_chatgpt/process/retry` | Restart app-server after failure |

Responses never include tokens. `authUrl` / `userCode` / `verificationUrl` are
cleared after completion, cancel, or failure and are not persisted in the browser.

### Process lifecycle

One app-server child per Accuretta runtime, started lazily for live status or
login. Shutdown is wired through provider background shutdown (`/api/shutdown`,
atexit, interrupt handlers). Ordinary shutdown does **not** delete Codex
credentials.

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
- [provider-smoke-test.md](provider-smoke-test.md) — manual milestone checklist
- [codex-chatgpt-auth-smoke-test.md](codex-chatgpt-auth-smoke-test.md) — ChatGPT / Codex auth checklist
- [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)

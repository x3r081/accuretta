# Inference providers

Accuretta routes inference through a small provider layer. **Local llama.cpp
remains the default.** This release also includes an **experimental OpenAI API
key** provider. No other cloud providers are supported.

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
- [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)

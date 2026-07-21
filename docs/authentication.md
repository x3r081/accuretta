# Authentication and credential storage

Accuretta keeps cloud credentials off the frontend. Local llama.cpp needs no
account. This milestone supports an **experimental OpenAI API-key** provider,
optional **experimental GitHub account login** (device flow, identity only —
not Copilot inference), and **experimental ChatGPT / Codex** through the
official Codex app-server. Accuretta does **not** store Codex tokens. Codex
**inference** is separate from authentication and is off unless
`ACCURETTA_CODEX_INFERENCE_ENABLED=1` (see [providers.md](providers.md)).

## Storage backends

| Backend | When | Location |
|---|---|---|
| macOS Keychain via `keyring` | Preferred on Darwin when keyring works | Service `Accuretta`, account `provider:<id>` |
| Secure file fallback | Keyring missing/unavailable | `data/auth/credentials.json` (owner-only `0o600`, atomic write + lock) |

Application startup does **not** fail if keyring is unavailable. Local inference
keeps working. `/api/providers` reports `secureCloudAuthAvailable` / `authDetail`
when cloud auth storage is limited.

Never commit credential files. See `.gitignore`.

## What is stored

`StoredCredential` may include access token, refresh token, expiry, token type,
scopes, small metadata (e.g. account label), and schema version. Avoid storing
unnecessary profile data.

## OAuth primitives (generic)

`auth/` includes PKCE, loopback callback (127.0.0.1 / ::1), config-driven code
exchange, refresh-with-skew, **RFC 8628 device authorization**
(`auth/device_flow.py`, `auth/device_models.py`), and redaction helpers. These are
for future providers and tests (`tests/fake_oauth_server.py`).

### Device authorization (backend-only)

Safe HTTP surface (never returns `device_code` or tokens):

| Method | Path |
|---|---|
| POST | `/api/providers/{id}/device/start` |
| GET | `/api/providers/{id}/device/status` |
| POST | `/api/providers/{id}/device/cancel` |

Start responses may include `userCode`, `verificationUri`,
`verificationUriComplete`, `expiresAt`, and `pollIntervalSeconds`. The bridge
polls the token endpoint in the background — `app.js` must not poll upstream
providers directly. Only one active device flow is kept per provider; a new
start cancels the prior poller. Shutdown cancels in-flight pollers.

Security rules:

- Browser auth-code flows use PKCE + state (`hmac.compare_digest`)
- Loopback only on localhost
- Short callback timeout
- **Refresh failure classification** (`auth/refresh_errors.py`):
  - *Permanent* (`invalid_grant`, revoked token, `invalid_client`, bare HTTP 401) → delete credentials and require reconnect
  - *Transient* (timeout, DNS, connection errors, HTTP 429 / 5xx, malformed temporary body) → **retain** credentials; do not use an already-expired access token; allow retry
  - *Configuration* (missing token URL / client ID) → retain credentials; report `ProviderNotConfigured`
- Logout / disconnect calls `AuthStore.delete(provider_id)` and cancels any pending device session
- `device_code` is never logged and never sent to the frontend

## Frontend security boundary

Provider HTTP responses are built by `providers.status` / `providers.management`
and asserted free of forbidden keys before return.

`app.js` must only see safe status DTOs. Tokens must never appear in:

- JSON API responses
- logs / exceptions shown to users
- settings JSON
- localStorage / sessionStorage
- test snapshots

## OpenAI API keys

OpenAI uses a user-supplied **API key** (not OAuth). Keys are stored as
`StoredCredential` with `token_type="api_key"` and
`metadata.credential_type="api_key"` in the `access_token` field for secure
storage reuse — **not** as a refresh token.

Connect via `POST /api/providers/openai/connect` with `{ "apiKey": "..." }`.
The key must never appear in settings JSON, provider status, logs, or
`StoredCredential.__repr__`.

Loopback / Tailscale deployments: treat API-key submission like any other
secret POST — prefer HTTPS when exposing Accuretta beyond localhost.

## Connect / disconnect (current UI)

- **Local llama.cpp:** disconnect is not applicable (button hidden/disabled).
- **OpenAI API:** password input + Connect/Update key; Disconnect removes the key.
- **GitHub account:** device-flow Connect GitHub (user code + verification URL);
  Disconnect removes the GitHub credential only. Cannot be selected for chat.
- **ChatGPT / Codex:** browser or device-code login via Codex app-server;
  Disconnect calls Codex `account/logout`. Tokens stay in Codex — never AuthStore.
  Cannot be selected for chat. Inference is not enabled in this milestone.
- **Example cloud (demo):** Connect disabled; Disconnect clears fake credentials if present.

## ChatGPT / Codex (Codex-managed)

Accuretta communicates only with `codex app-server` JSON-RPC over **stdio**.
Codex owns OAuth registration, browser/device callbacks, access/refresh tokens,
persistence, refresh, and logout. Accuretta does **not** host a custom OAuth
callback for ChatGPT. Accuretta:

- never reads or writes Codex credential files
- never copies Codex tokens into AuthStore or settings
- never logs `access_token`, `refresh_token`, or `Authorization` values
- never reuses Hermes / VS Code / GitHub CLI / other third-party OAuth client IDs
- never calls undocumented private OpenAI endpoints for this flow

**Authentication** (sign-in status) works without the inference flag.
**Inference** requires `ACCURETTA_CODEX_INFERENCE_ENABLED=1` plus a ready CLI
session; select **Codex via ChatGPT** explicitly in Settings. Disconnect or
expired sign-in makes Codex unselectable and returns actionable errors — Accuretta
does not silently switch to local llama.

See [codex-chatgpt-auth-smoke-test.md](codex-chatgpt-auth-smoke-test.md),
[codex-inference-checklist.md](codex-inference-checklist.md), and
[providers.md](providers.md).

## Fake-provider tests

```bash
python3 -m unittest discover -s tests -v
```

Codex tests use `tests/fake_codex_app_server.py` (stdio JSONL). No test contacts
OpenAI or performs a real ChatGPT login.

## Attribution

Generic OAuth/persistence patterns were adapted from Nous Research Hermes Agent
(MIT). See [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). Accuretta does
not reuse Hermes OAuth client IDs or registrations. The Codex client in this
repository is independently written against the documented app-server protocol;
it does not vendor the Codex source tree.

## Related

- [providers.md](providers.md)
- [codex-inference-checklist.md](codex-inference-checklist.md)
- [codex-chatgpt-auth-smoke-test.md](codex-chatgpt-auth-smoke-test.md)
- [codex-workspace-security.md](codex-workspace-security.md)

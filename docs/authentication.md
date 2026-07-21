# Authentication and credential storage

Accuretta keeps cloud credentials off the frontend. Local llama.cpp needs no
account. **No external OAuth provider is enabled in this release.**

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
exchange, refresh-with-skew, and redaction helpers. These are for future
providers and tests (`tests/fake_oauth_server.py`).

Security rules:

- Browser auth-code flows use PKCE + state (`hmac.compare_digest`)
- Loopback only on localhost
- Short callback timeout
- **Refresh failure classification** (`auth/refresh_errors.py`):
  - *Permanent* (`invalid_grant`, revoked token, `invalid_client`, bare HTTP 401) → delete credentials and require reconnect
  - *Transient* (timeout, DNS, connection errors, HTTP 429 / 5xx, malformed temporary body) → **retain** credentials; do not use an already-expired access token; allow retry
  - *Configuration* (missing token URL / client ID) → retain credentials; report `ProviderNotConfigured`
- Logout / disconnect calls `AuthStore.delete(provider_id)`

## Frontend security boundary

Provider HTTP responses are built by `providers.status` / `providers.management`
and asserted free of forbidden keys before return.

`app.js` must only see safe status DTOs. Tokens must never appear in:

- JSON API responses
- logs / exceptions shown to users
- settings JSON
- localStorage / sessionStorage
- test snapshots

## Connect / disconnect (current UI)

- **Local llama.cpp:** disconnect is not applicable (button hidden/disabled).
- **Example cloud (demo):** Connect disabled; Disconnect clears fake credentials if present.
- Live Connect for real providers is **not** offered in this phase.

## Fake-provider tests

```bash
python3 -m unittest tests.test_oauth_flow tests.test_auth_store tests.test_providers_api -v
```

These use an in-process fake OAuth server and temporary credential files. No
test contacts a real external provider.

## Attribution

Generic OAuth/persistence patterns were adapted from Nous Research Hermes Agent
(MIT). See [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). Accuretta does
not reuse Hermes OAuth client IDs or registrations.

## Related

- [providers.md](providers.md)

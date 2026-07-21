# Inference providers

Accuretta routes inference through a small provider layer. **Local llama.cpp is
the only inference-capable provider in this release.** Cloud providers are not
supported yet.

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

- `providers/` — definitions, registry, status DTOs, `LocalLlamaProvider`
- `auth/` — credential storage and generic OAuth primitives (unused by UI until a real provider ships)

## Local provider

- **id:** `local_llama`
- **auth:** none
- **default:** yes
- Uses existing llama-server lifecycle, GGUF discovery, streaming, tools, and cancellation in `bridge.py`.

Chat orchestration (`run_chat_turn`) still owns the agent loop. Phase 5 adds a
**compatibility gate**: HTTP chat resolves the selected provider and only
continues when it is usable local llama. The SSE stream protocol is unchanged.

## Provider selection

Settings key: `provider_id` (default `"local_llama"`).

| Stored value | Behavior |
|---|---|
| missing / empty | Local llama.cpp |
| `local_llama` | Local llama.cpp |
| unknown id | Fall back to local llama; sanitized warning on `/api/providers` |
| disabled / unavailable | Kept in settings only if forced by hand; **chat is blocked** with HTTP 409 — Accuretta will not silently send chat to an external backend |

Credentials are **never** stored in `settings.json`.

## Demonstration provider

`example_cloud` is registered as **experimental and unavailable** so the API/UI
can be exercised without network calls or OAuth registration.

- Connect is rejected
- Model listing returns 409
- Selection returns 409
- Disconnect removes any test credentials from AuthStore (no-op if none)

Do not treat it as a usable cloud backend.

## HTTP API (safe fields only)

### `GET /api/providers`

```json
{
  "providers": [ /* safe status objects */ ],
  "selectedProviderId": "local_llama",
  "defaultProviderId": "local_llama",
  "warning": null,
  "authBackend": "keychain",
  "secureCloudAuthAvailable": true,
  "authDetail": null
}
```

### Safe status object

May include: `providerId`, `displayName`, `apiMode`, `authType`,
`authenticated`, `available`, `selected`, `isDefault`, `experimental`,
`enabled`, `expiresAt`, `accountLabel`, `supportsModelListing`,
`capabilities`, `disabledReason`, `error`.

Must never include: access/refresh tokens, client secrets, code verifiers,
authorization headers, raw credential objects, or raw provider responses.

### Other routes

| Method | Path | Notes |
|---|---|---|
| GET | `/api/providers/{id}/status` | One safe status |
| GET | `/api/providers/{id}/models` | Local discovery only today |
| POST | `/api/providers/{id}/select` | Persists `provider_id`; rejects unavailable |
| POST | `/api/providers/{id}/disconnect` | No-op for local; deletes AuthStore entry otherwise |
| POST | `/api/providers/{id}/connect` | Always 409 in this release |

Error codes use sanitized `providers.errors` classes (404 unknown, 409 unavailable, …).

## Frontend boundary

`app.js` may call the provider endpoints and render safe status only. It must
never receive or display tokens. Provider UI failures must not block local chat.

## Adding a future provider

1. Register a `ProviderDefinition` (+ factory when inference-ready).
2. Implement AuthStore-backed connect only with Accuretta’s own OAuth/API-key registration.
3. Expose status only through `providers.status.build_safe_provider_status`.
4. Do not reuse Hermes or third-party OAuth client IDs.
5. Keep local llama as the default until the user explicitly selects another **available** provider.

## Related

- [authentication.md](authentication.md)
- [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)

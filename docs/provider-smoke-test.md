# Provider milestone smoke test

Manual checklist for the current Accuretta provider milestone on
`feat/oauth-provider-support`.

This release supports:

- **Local llama.cpp** — default inference
- **OpenAI API key** — experimental cloud inference
- **GitHub account login** — experimental, account only (not Copilot, not chat)
- **Codex via ChatGPT** — experimental Codex-managed auth; optional gated
  inference (`ACCURETTA_CODEX_INFERENCE_ENABLED`) — see
  [codex-inference-checklist.md](codex-inference-checklist.md)

## Prerequisites

- Accuretta starts (`python3 bridge.py` or your usual launcher)
- Optional: a GGUF model + `llama-server` for local inference
- Optional: an OpenAI API key for the cloud path
- GitHub OAuth App client ID is **not** required for this smoke test

## Safe diagnostics

After startup, open Settings → Provider, or call:

```bash
curl -sS http://127.0.0.1:8787/api/providers | python3 -m json.tool
```

Inspect `diagnostics` (and each provider’s `available` / `disabledReason`):

| Field | Meaning |
|---|---|
| `localProviderAvailable` | Local provider is registered/enabled |
| `openaiCredentialStored` / `openaiCredentialValidated` | API key state (never the key) |
| `githubConfigured` / `githubAvailable` | Accuretta GitHub OAuth App configured |
| `githubDisabledReason` | Why GitHub is unavailable (if so) |
| `authBackend` | `keychain` or `file` |
| `secureCloudAuthAvailable` | Whether Keychain-backed storage is available |

Responses must never contain API keys, tokens, `device_code`, or Authorization headers.

## 1. Local provider smoke test

1. Start Accuretta.
2. Open **Settings → Provider**.
3. Confirm **Local llama.cpp** is selected (default).
4. If a model is configured, send a short local prompt (e.g. “Reply with the word ping”).
5. Confirm a streamed reply arrives.

If llama-server / model is missing, Accuretta should still start; local chat may fail clearly until configured. That is not a provider-layer regression.

## 2. OpenAI API-key smoke test

1. In Provider settings, select **OpenAI API**.
2. Paste an API key into the password field → **Connect** / **Update key**.
3. Confirm the key field **clears** immediately after submit.
4. Confirm status shows key stored / validated (as appropriate). Confirm the key is **not** shown again.
5. **Refresh models** → pick a chat model.
6. **Use provider**.
7. Send a short streaming text prompt.
8. Optionally send one safe tool-capable prompt (e.g. list workspace files if tools are enabled).
9. Start a longer prompt and **Cancel** — generation should stop.
10. Restart Accuretta.
11. Confirm `provider_id` / OpenAI model selection persist; API key remains hidden.
12. **Disconnect** OpenAI.
13. With OpenAI still selected, confirm a new chat is **blocked** (clear error / 401), not silently routed to local.
14. Select **Local llama.cpp** → **Use provider**.
15. Confirm local chat still works.

Do not automate live OpenAI calls in unit tests.

## 3. GitHub (expected unconfigured behavior)

Without `ACCURETTA_GITHUB_CLIENT_ID`:

- GitHub appears under **GitHub account** as unavailable / experimental
- Connect is disabled
- A safe disabled reason mentions configuring an Accuretta-owned OAuth App
- Local and OpenAI continue to work
- GitHub cannot be selected for chat

Connecting GitHub requires the project owner to configure Accuretta’s own OAuth App later. Missing GitHub config is **not** a merge blocker.

## 4. Cancellation / shutdown

- Cancel an in-flight OpenAI or local stream via the UI cancel control.
- Quit Accuretta (Ctrl+C or in-app shutdown). Device-flow pollers must be cancelled; llama-server should stop.

## 5. Troubleshooting

| Symptom | Check |
|---|---|
| OpenAI chat 401 after disconnect | Select Local or reconnect a key |
| OpenAI connect fails 401/403 | Key rejected — not stored |
| OpenAI connect rate-limited | Key may be stored unvalidated — retry model list later |
| Models list empty | Key missing/invalid, or filter excluded non-chat models |
| GitHub Connect disabled | Set `ACCURETTA_GITHUB_CLIENT_ID` (Accuretta-owned app only) |
| `secureCloudAuthAvailable: false` | File fallback is OK for smoke; Keychain preferred on macOS |

## 6. Rollback to local

1. Settings → Provider → Local llama.cpp → **Use provider**.
2. Confirm chat works locally.
3. Optionally Disconnect OpenAI / GitHub — does not affect local credentials.

## Related

- [providers.md](providers.md)
- [authentication.md](authentication.md)

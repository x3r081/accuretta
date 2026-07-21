# Third-Party Notices

This file documents third-party code that Accuretta adapts or is inspired by.

Accuretta itself remains licensed under the PolyForm Noncommercial License 1.0.0
(see `LICENSE`). Nothing in this file weakens or replaces that license.

---

## Nous Research — Hermes Agent (MIT)

Repository: https://github.com/NousResearch/hermes-agent  
Copyright (c) Nous Research  
License: MIT (see below)

Accuretta adapts **narrow, generic OAuth and credential-persistence primitives**
from Hermes Agent. Accuretta does **not** copy Hermes' monolithic `hermes_cli/auth.py`,
provider branding, documentation, or OAuth client registrations.

### Directly adapted (with Accuretta-specific refactor)

| Accuretta file | Hermes source patterns |
|---|---|
| `auth/pkce.py` | `hermes_cli/auth.py` `_oauth_pkce_*`; Honcho `plugins/memory/honcho/oauth_flow.py` `_pkce` |
| `auth/loopback.py` | Honcho loopback bind/capture; Spotify redirect validation ideas in `hermes_cli/auth.py` |
| `auth/oauth_client.py` | Authorize URL assembly, authorization-code exchange, RFC 8628 device-code polling (`_poll_for_token`) |
| `auth/token_refresh.py` | `_is_expiring`, `_coerce_ttl_seconds`, `_parse_iso_timestamp`, refresh persistence behavior |
| `auth/atomic.py` | `_save_auth_store` O_EXCL/0o600/fsync; `utils.atomic_replace` |
| `auth/lock.py` | `_file_lock` / `_auth_store_lock` flock patterns |

### Inspired by Hermes (rewritten for Accuretta)

- Token fingerprint / sensitive-value redaction ideas → `auth/redact.py`
- Auth store load/corrupt-backup behavior → `auth/file_store.py`
- Structured auth error codes (Accuretta owns the class set) → `providers/errors.py`

### Explicitly not copied

- Hermes `PROVIDER_REGISTRY` and provider display branding
- Hardcoded OAuth client IDs / redirect URIs / secrets (Codex, Copilot VS Code app, Anthropic Claude Code, MiniMax, Nous portal, etc.)
- ChatGPT / Codex device-login product flow as an “official OpenAI” integration
- Unofficial Copilot `copilot_internal` token exchange and editor user-agent spoofing
- Spotify, Honcho product UI, MCP dashboard OAuth manager, and unrelated Hermes application code

**Important:** Hermes OAuth client IDs, redirect URIs, and scopes are **not**
automatically reusable by Accuretta. Accuretta must use its own provider
registrations (or user-supplied API keys) before enabling a real cloud provider.

### MIT License (Hermes Agent)

```
MIT License

Copyright (c) Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Newly written for Accuretta (not from Hermes)

- `providers/` package interfaces, registry, runtime resolver, `LocalLlamaProvider`
- `auth/models.py`, `auth/store.py`, `auth/keychain_store.py`
- `tests/fake_oauth_server.py` and Accuretta auth/provider unit tests

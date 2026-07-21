# ChatGPT / Codex authentication smoke test

Manual checklist for Accuretta’s **authentication-only** Codex integration.
Codex owns OAuth, tokens, and persistence. Accuretta talks only to
`codex app-server` over stdio JSON-RPC.

**Tested against:** Codex CLI `0.144.6` (local development machine).

## Prerequisites

1. Install or verify the official Codex CLI (`codex` on `PATH`, or set
   `ACCURETTA_CODEX_BIN` to an absolute executable).
2. Record `codex --version`.
3. Confirm `codex app-server --help` lists the stdio transport (default).
4. Do **not** start a ChatGPT login during automated unit tests.

## Checklist

1. Install or verify official Codex CLI.
2. Record `codex --version` (expected during this milestone: `0.144.6` or newer
   with account methods).
3. Start Accuretta.
4. Open **Settings → Provider**.
5. Verify the **ChatGPT / Codex** section shows CLI detected / version /
   availability (or a sanitized “not installed” / start-failure reason).
6. Test **Sign in with ChatGPT** (browser flow). Complete login in the browser.
7. Verify signed-in status and **plan type** when Codex supplies one.
8. Restart Accuretta.
9. Confirm signed-in status persists (Codex credential persistence — Accuretta
   must not have copied tokens into AuthStore).
10. **Disconnect** / log out via Accuretta (calls `account/logout`).
11. Test **Use device code** login.
12. **Cancel** a device-code login before completing it.
13. Complete device-code login.
14. Restart Accuretta again; confirm still signed in via Codex.
15. Confirm **local llama** remains usable for chat.
16. Confirm **OpenAI API-key** provider remains usable (separate credential).
17. Confirm Accuretta never displays an access/refresh token or JWT.
18. Confirm **ChatGPT / Codex is not selectable** in the chat provider dropdown.

## Expected boundaries

- No reads/writes of Codex credential files from Accuretta.
- No Codex tokens in AuthStore, settings JSON, or browser storage.
- OpenAI API-key and GitHub account credentials remain independent.
- Codex inference, threads, turns, tools, and model listing are **not** enabled.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| CLI not detected | Install Codex; or set `ACCURETTA_CODEX_BIN` |
| Provider unavailable | App-server failed to start / initialize |
| Login stuck pending | Complete or cancel in UI; check Codex CLI health |
| Plan type missing | Codex may omit `planType` — Accuretta shows it only when supplied |

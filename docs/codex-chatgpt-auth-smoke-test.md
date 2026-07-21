# ChatGPT / Codex authentication smoke test

Manual checklist for Accuretta’s **Codex-managed ChatGPT authentication**.
Codex owns OAuth, tokens, and persistence. Accuretta talks only to
`codex app-server` over stdio JSON-RPC.

For **inference** (streaming chat via Codex), see
[codex-inference-checklist.md](codex-inference-checklist.md) and
[providers.md](providers.md). Auth and inference are separate: you can sign in
with the inference flag off.

**Tested against:** Codex CLI `0.144.6` (local development machine).

## Prerequisites

1. Install or verify the official Codex CLI (`codex` on `PATH`, or set
   `ACCURETTA_CODEX_BIN` to an absolute executable).
2. Record `codex --version`.
3. Confirm `codex app-server --help` lists the stdio transport (default).
4. Do **not** start a ChatGPT login during automated unit tests.

## Checklist

1. Install or verify official Codex CLI.
2. Record `codex --version` (expected during verification: `0.144.6` or a build
   with the same account methods).
3. Start Accuretta **without** requiring inference:
   `python3 bridge.py` (flag unset / off).
4. Open **Settings → Provider**.
5. Verify the **ChatGPT / Codex** section shows CLI detected / version /
   availability (or a sanitized “not installed” / start-failure reason).
6. Test **Sign in with ChatGPT** (browser flow). Complete login in the browser
   (Codex-owned OAuth — Accuretta does not run a custom callback).
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
18. With inference flag **off**, confirm Codex is **not** selectable for chat
    (auth ≠ inference).
19. With `ACCURETTA_CODEX_INFERENCE_ENABLED=1`, confirm Codex becomes selectable
    when ready; follow [codex-inference-checklist.md](codex-inference-checklist.md).

## Expected boundaries

- No reads/writes of Codex credential files from Accuretta.
- No Codex tokens in AuthStore, settings JSON, or browser storage.
- OpenAI API-key and GitHub account credentials remain independent.
- No Accuretta custom OAuth callback for ChatGPT.
- Stdio-only app-server transport.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| CLI not detected | Install Codex; or set `ACCURETTA_CODEX_BIN` |
| Provider unavailable | App-server failed to start / initialize |
| Login stuck pending | Complete or cancel in UI; check Codex CLI health |
| Plan type missing | Codex may omit `planType` — Accuretta shows it only when supplied |
| Not selectable for chat | Inference flag off, or not signed in / not ready |
| Port already in use | Other process on `ACCURETTA_PORT`; set a free port — launcher requires Accuretta `/api/health` (`app: accuretta`), not bare TCP |

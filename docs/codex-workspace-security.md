# Codex workspace security model

Accuretta binds Codex to the **active Accuretta workspace folder** using only
the official Codex app-server `thread/start` parameter `cwd`. Accuretta never
sends the workspace tree as prompt text.

## What Codex receives

| Field | Value |
|---|---|
| `cwd` | Normalized, symlink-resolved Accuretta workspace root (first configured folder, or the path bound to that chat) |
| `sandbox` | `read-only` when write mode is **Chat only**; otherwise `workspace-write` |
| `approvalPolicy` | `untrusted` for **Ask before every write** (forces approval RPCs on Codex CLI 0.144.6); otherwise `on-request` |

`danger-full-access` is **never** sent. If a caller requests it, Accuretta clamps
to `workspace-write`.

## Codex write modes (`codex_write_mode`)

| Mode | Native file writes | Shell | Sandbox |
|---|---|---|---|
| **Chat only** (`chat_only`) | Always declined | Always declined | `read-only` |
| **Ask before every write** (`ask`, default) | Accuretta approval UI (`approvalPolicy: untrusted`); outside workspace declined | Accuretta approval UI | `workspace-write` |
| **Always allow within workspace** (`workspace_auto`) | Auto-accept paths inside workspace; outside declined | Still requires approval | `workspace-write` |

Shell auto-approve is **not** enabled by any of these modes. OAuth / ChatGPT
token handling is unchanged.

Changing `codex_write_mode` clears bound Codex thread ids so the next turn
starts a fresh thread with the matching sandbox.

## What Accuretta rejects as `cwd`

- Empty workspace configuration
- Filesystem root (`/` or drive root)
- The user home directory itself (`~` / `$HOME`)
- Paths outside configured Accuretta workspace folders
- Blocked / protected locations (Accuretta path deny list)
- Non-directories / unresolvable paths

Path traversal (`..`) is resolved via `Path.resolve`; the result must still lie
under an Accuretta workspace root.

## Separation of capabilities

| Capability | Codex path | Accuretta local path |
|---|---|---|
| Conversational inference | Yes (ChatGPT / Codex) | Yes (local llama / OpenAI API) |
| File inspection / edits via Accuretta tools | No | Yes, workspace-gated + approvals |
| Native Codex file change / shell approvals | Per `codex_write_mode` (never outside workspace) | n/a |
| Unrestricted writes because user is signed in | **No** | **No** |

Codex server requests such as `item/fileChange/requestApproval` and
`item/commandExecution/requestApproval` are handled by Accuretta’s policy in
`codex/approvals.py` (and may surface as Accuretta approval cards in **ask**
mode). Paths outside the validated workspace are always declined.

## Per-conversation isolation

Each Accuretta chat may persist:

- `codex_thread_id` — remote thread resume key (non-secret)
- `codex_cwd` — the workspace root used when the thread was created

Unrelated chats do not share thread ids or cwd bindings. Changing the Accuretta
workspace does not silently retarget an existing Codex thread to home/root.

## UI

Settings → ChatGPT / Codex shows the resolved workspace path and the write-mode
control. Chat turns emit a `provider` event that includes the safe workspace DTO
so the streaming bubble can display which project root Codex is using.

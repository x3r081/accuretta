# Codex workspace security model

Accuretta binds Codex to the **active Accuretta workspace folder** using only
the official Codex app-server `thread/start` parameter `cwd`. Accuretta never
sends the workspace tree as prompt text.

## What Codex receives

| Field | Value |
|---|---|
| `cwd` | Normalized, symlink-resolved Accuretta workspace root (first configured folder, or the path bound to that chat) |
| `sandbox` | Always `read-only` |
| `approvalPolicy` | Always `on-request` |

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
| Native Codex file change / shell approvals | **Always declined** | n/a |
| Unrestricted writes because user is signed in | **No** | **No** |

Codex server requests such as `item/fileChange/requestApproval` and
`item/commandExecution/requestApproval` are answered with **decline**. Accuretta
does not bridge those into Accuretta’s approval UI. Therefore Codex remains
**advisory / chat-only** for tool and write actions in this build.

## Per-conversation isolation

Each Accuretta chat may persist:

- `codex_thread_id` — remote thread resume key (non-secret)
- `codex_cwd` — the workspace root used when the thread was created

Unrelated chats do not share thread ids or cwd bindings. Changing the Accuretta
workspace does not silently retarget an existing Codex thread to home/root.

## UI

Settings → ChatGPT / Codex shows the resolved workspace path (or “no workspace”).
Chat turns emit a `provider` event that includes the safe workspace DTO so the
streaming bubble can display which project root Codex is using.

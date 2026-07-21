"""Codex native write/shell approval policy for Accuretta.

Codex app-server asks Accuretta (via JSON-RPC server requests) whether to
allow file changes and shell commands. Accuretta decides based on the
user's ``codex_write_mode`` setting and the validated workspace cwd.

Modes:
- ``chat_only`` — decline all native file/shell approvals (sandbox read-only)
- ``ask`` — prompt via Accuretta's approval UI for every in-workspace write
  (recommended default); shell always prompts
- ``workspace_auto`` — auto-accept file writes strictly inside the active
  workspace; shell still prompts unless a future explicit shell opt-in exists

Never accept writes outside the validated workspace. Never enable
``danger-full-access``. OAuth / token handling is unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

WRITE_MODE_CHAT_ONLY = "chat_only"
WRITE_MODE_ASK = "ask"
WRITE_MODE_WORKSPACE_AUTO = "workspace_auto"

VALID_WRITE_MODES = frozenset({
    WRITE_MODE_CHAT_ONLY,
    WRITE_MODE_ASK,
    WRITE_MODE_WORKSPACE_AUTO,
})

# Recommended default: ask before every write.
DEFAULT_WRITE_MODE = WRITE_MODE_ASK

MODE_LABELS = {
    WRITE_MODE_CHAT_ONLY: "Chat only",
    WRITE_MODE_ASK: "Ask before every write",
    WRITE_MODE_WORKSPACE_AUTO: "Always allow writes within workspace",
}

KIND_FILE_CHANGE = "codex_file_change"
KIND_SHELL = "codex_shell"


def normalize_codex_write_mode(raw: Any) -> str:
    if not isinstance(raw, str):
        return DEFAULT_WRITE_MODE
    mode = raw.strip().lower().replace("-", "_")
    aliases = {
        "advisory": WRITE_MODE_CHAT_ONLY,
        "advisory_chat_only": WRITE_MODE_CHAT_ONLY,
        "chat": WRITE_MODE_CHAT_ONLY,
        "ask_before_write": WRITE_MODE_ASK,
        "ask_before_every_write": WRITE_MODE_ASK,
        "auto": WRITE_MODE_WORKSPACE_AUTO,
        "always_allow": WRITE_MODE_WORKSPACE_AUTO,
        "workspace": WRITE_MODE_WORKSPACE_AUTO,
    }
    mode = aliases.get(mode, mode)
    return mode if mode in VALID_WRITE_MODES else DEFAULT_WRITE_MODE


def get_codex_write_mode(settings: Optional[Mapping[str, Any]] = None) -> str:
    if settings is None:
        try:
            import bridge
            settings = bridge.get_settings()
        except Exception:
            return DEFAULT_WRITE_MODE
    if not isinstance(settings, Mapping):
        return DEFAULT_WRITE_MODE
    return normalize_codex_write_mode(settings.get("codex_write_mode"))


def sandbox_for_write_mode(mode: str) -> str:
    """Map write mode → Codex thread sandbox (never danger-full-access)."""
    m = normalize_codex_write_mode(mode)
    if m == WRITE_MODE_CHAT_ONLY:
        return "read-only"
    return "workspace-write"


def approval_policy_for_write_mode(mode: str) -> str:
    """Map write mode → Codex approvalPolicy.

    Codex CLI 0.144.6 with ``workspace-write`` + ``on-request`` does **not**
    emit ``item/fileChange/requestApproval`` for in-workspace writes (they
    auto-apply). Accuretta's "ask" mode therefore uses ``untrusted`` so the
    app-server always requests approval and Accuretta can show the UI.
    """
    m = normalize_codex_write_mode(mode)
    if m == WRITE_MODE_ASK:
        return "untrusted"
    return "on-request"


# Bounded wait for Accuretta UI while Codex holds a server request open.
APPROVAL_UI_TIMEOUT_S = 90

KIND_PERMISSIONS = "codex_permissions"


def mode_allows_coding_actions(mode: str) -> bool:
    return normalize_codex_write_mode(mode) != WRITE_MODE_CHAT_ONLY


def _normalize_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw or "\x00" in raw:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(raw))
    try:
        return str(Path(expanded).resolve(strict=False))
    except Exception:
        try:
            return os.path.abspath(expanded)
        except Exception:
            return ""


def path_under_workspace(path: str, workspace_cwd: Optional[str]) -> bool:
    """True iff ``path`` resolves under the validated workspace root."""
    root = _normalize_path(workspace_cwd or "")
    target = _normalize_path(path)
    if not root or not target:
        return False
    try:
        if os.name == "nt":
            common = os.path.commonpath([root.lower(), target.lower()])
            return common == root.lower()
        common = os.path.commonpath([root, target])
        return common == root
    except Exception:
        return False


def extract_file_change_paths(
    params: Optional[Mapping[str, Any]],
    *,
    item_cache: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Collect candidate paths from an approval request + cached item/started."""
    params = params or {}
    paths: list[str] = []

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                _add(item)

    _add(params.get("grantRoot") or params.get("grant_root"))
    _add(params.get("path"))
    _add(params.get("paths"))
    for key in ("filePath", "file_path", "targetPath", "target_path"):
        _add(params.get(key))

    changes = params.get("changes")
    if isinstance(changes, list):
        for ch in changes:
            if isinstance(ch, dict):
                _add(ch.get("path"))

    item_id = params.get("itemId") or params.get("item_id")
    if item_cache and isinstance(item_id, str):
        cached = item_cache.get(item_id)
        if isinstance(cached, dict):
            _add(cached.get("grantRoot") or cached.get("grant_root"))
            cached_changes = cached.get("changes")
            if isinstance(cached_changes, list):
                for ch in cached_changes:
                    if isinstance(ch, dict):
                        _add(ch.get("path"))
            _add(cached.get("path"))
            _add(cached.get("paths"))

    # De-dupe preserving order.
    seen = set()
    out: list[str] = []
    for p in paths:
        key = p.lower() if os.name == "nt" else p
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def extract_command_cwd(params: Optional[Mapping[str, Any]]) -> Optional[str]:
    params = params or {}
    for key in ("cwd", "workingDirectory", "working_directory"):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def extract_command_text(params: Optional[Mapping[str, Any]]) -> str:
    params = params or {}
    cmd = params.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd.strip()
    if isinstance(cmd, (list, tuple)):
        parts = [str(x) for x in cmd if x is not None]
        return " ".join(parts).strip()
    return ""


def all_paths_inside_workspace(
    paths: Sequence[str],
    workspace_cwd: Optional[str],
) -> bool:
    if not paths:
        return False
    return all(path_under_workspace(p, workspace_cwd) for p in paths)


def _decline() -> dict:
    return {"decision": "decline"}


def _accept() -> dict:
    return {"decision": "accept"}


def _legacy_denied() -> dict:
    return {"decision": "denied"}


def _legacy_approved() -> dict:
    return {"decision": "approved"}


ApprovalRequester = Callable[..., dict]


def decide_file_change(
    *,
    mode: str,
    params: Optional[Mapping[str, Any]],
    workspace_cwd: Optional[str],
    item_cache: Optional[Mapping[str, Any]] = None,
    request_approval: Optional[ApprovalRequester] = None,
    timeout_s: int = APPROVAL_UI_TIMEOUT_S,
    trust_writes: bool = False,
) -> dict:
    """Decide a Codex fileChange / applyPatch approval.

    Codex 0.144.6 ``item/fileChange/requestApproval`` params typically carry
    ``threadId``, ``turnId``, ``itemId``, ``reason``, ``grantRoot``,
    ``startedAtMs`` — not the file paths. Paths come from the preceding
    ``item/started`` fileChange notification (``item_cache``).
    """
    m = normalize_codex_write_mode(mode)
    if m == WRITE_MODE_CHAT_ONLY:
        return _decline()

    if not workspace_cwd or not _normalize_path(workspace_cwd):
        return _decline()

    paths = extract_file_change_paths(params, item_cache=item_cache)
    grant = None
    if params:
        grant = params.get("grantRoot") or params.get("grant_root")
    if isinstance(grant, str) and grant.strip() and not path_under_workspace(grant, workspace_cwd):
        return _decline()

    if paths and not all_paths_inside_workspace(paths, workspace_cwd):
        return _decline()

    # workspace_auto: accept when every known path is inside the workspace, or
    # when Codex only asks for a session grant under the workspace root / cwd
    # (sandbox is already workspace-write). Fail closed if paths point outside.
    if m == WRITE_MODE_WORKSPACE_AUTO:
        if paths:
            return _accept() if all_paths_inside_workspace(paths, workspace_cwd) else _decline()
        if isinstance(grant, str) and grant.strip():
            return _accept() if path_under_workspace(grant, workspace_cwd) else _decline()
        # No explicit paths — sandbox confines writes to cwd; allow.
        return _accept()

    # ask mode — Trust writes may auto-approve in-workspace file changes only.
    summary_paths = paths or (
        [grant] if isinstance(grant, str) and grant.strip() else [workspace_cwd]
    )
    if trust_writes and all_paths_inside_workspace(
        [p for p in summary_paths if p], workspace_cwd
    ):
        return _accept()

    if request_approval is None:
        return _decline()
    preview = ", ".join(str(p) for p in summary_paths[:4])
    if len(summary_paths) > 4:
        preview += f" (+{len(summary_paths) - 4} more)"
    reason = ""
    if params and isinstance(params.get("reason"), str):
        reason = params.get("reason") or ""
    try:
        result = request_approval(
            "Codex wants to write files",
            preview or workspace_cwd,
            {
                "kind": KIND_FILE_CHANGE,
                "paths": list(summary_paths),
                "path": summary_paths[0] if summary_paths else workspace_cwd,
                "workspace": workspace_cwd,
                "reason": reason,
                "provider": "codex_chatgpt",
            },
            timeout_s,
        )
    except Exception:
        return _decline()
    decision = (result or {}).get("decision")
    status = (result or {}).get("status")
    if decision == "approve":
        return _accept()
    if status == "timeout":
        return _decline()
    return _decline()


def decide_command_execution(
    *,
    mode: str,
    params: Optional[Mapping[str, Any]],
    workspace_cwd: Optional[str],
    request_approval: Optional[ApprovalRequester] = None,
    timeout_s: int = APPROVAL_UI_TIMEOUT_S,
    shell_auto_enabled: bool = False,
) -> dict:
    """Decide a Codex shell / commandExecution approval.

    Shell always requires an explicit Accuretta approval unless
    ``shell_auto_enabled`` is True (reserved; default False — not exposed yet).
    Commands whose cwd escapes the workspace are always declined.
    Trust writes never auto-approves shell.
    """
    m = normalize_codex_write_mode(mode)
    if m == WRITE_MODE_CHAT_ONLY:
        return _decline()

    if not workspace_cwd or not _normalize_path(workspace_cwd):
        return _decline()

    cmd_cwd = extract_command_cwd(params) or workspace_cwd
    if not path_under_workspace(cmd_cwd, workspace_cwd):
        return _decline()

    if shell_auto_enabled:
        return _accept()

    if request_approval is None:
        return _decline()
    command = extract_command_text(params) or "(shell command)"
    try:
        result = request_approval(
            "Codex wants to run a shell command",
            command,
            {
                "kind": KIND_SHELL,
                "command": command,
                "cwd": cmd_cwd,
                "workspace": workspace_cwd,
                "provider": "codex_chatgpt",
            },
            timeout_s,
        )
    except Exception:
        return _decline()
    decision = (result or {}).get("decision")
    if decision == "approve":
        return _accept()
    return _decline()


def decide_permissions_request(
    *,
    mode: str,
    params: Optional[Mapping[str, Any]],
    workspace_cwd: Optional[str],
) -> dict:
    """Handle ``item/permissions/requestApproval`` — never grant outside workspace."""
    m = normalize_codex_write_mode(mode)
    if m == WRITE_MODE_CHAT_ONLY:
        return _decline()
    if not workspace_cwd or not _normalize_path(workspace_cwd):
        return _decline()
    params = params or {}
    grant = params.get("grantRoot") or params.get("grant_root")
    if isinstance(grant, str) and grant.strip():
        if not path_under_workspace(grant, workspace_cwd):
            return _decline()
        # In-workspace grant: auto modes may accept; ask still declines elevation
        # grants without an explicit Accuretta card (fail closed for permissions).
        if m == WRITE_MODE_WORKSPACE_AUTO:
            return _accept()
        return _decline()
    # No grant root — decline (do not widen sandbox silently).
    return _decline()


def map_legacy_decision(modern: Mapping[str, Any]) -> dict:
    """Map v2 accept/decline → legacy approved/denied for applyPatch/execCommand."""
    if (modern or {}).get("decision") == "accept":
        return _legacy_approved()
    return _legacy_denied()


def cache_file_change_item(item: Any) -> Optional[tuple[str, dict]]:
    """Extract (itemId, cache_entry) from an item/started|completed payload."""
    if not isinstance(item, dict):
        return None
    if str(item.get("type") or "") != "fileChange":
        return None
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        return None
    entry = {
        "changes": item.get("changes") if isinstance(item.get("changes"), list) else [],
        "path": item.get("path"),
        "paths": item.get("paths"),
        "grantRoot": item.get("grantRoot") or item.get("grant_root"),
    }
    return item_id.strip(), entry


def describe_write_mode(mode: str) -> str:
    m = normalize_codex_write_mode(mode)
    return MODE_LABELS.get(m, MODE_LABELS[DEFAULT_WRITE_MODE])

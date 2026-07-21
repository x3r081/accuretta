"""Safe Accuretta → Codex workspace (cwd) binding.

Newly written for Accuretta. Uses only the official Codex ``thread/start``
``cwd`` parameter. Never dumps the workspace into prompt text.

Security model (summary):
- Codex conversational inference may run with or without a cwd.
- Native Codex file/shell actions are **declined** (advisory / chat-only).
- Accuretta's own tools + approval gates are unchanged and are not used
  by the Codex provider path.
- cwd is always an Accuretta-configured workspace folder (normalized,
  symlink-resolved). Home directory and filesystem root are rejected.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .status import assert_safe_provider_payload

# In-memory Accuretta chat_id → bound Codex cwd (mirrors chats.json codex_cwd).
_CWD_BY_CHAT: dict[str, str] = {}
_CWD_LOCK = threading.Lock()

FORBIDDEN_REASON_HOME = "workspace must not be the user home directory"
FORBIDDEN_REASON_ROOT = "workspace must not be the filesystem root"
FORBIDDEN_REASON_MISSING = "no Accuretta workspace folder is selected"
FORBIDDEN_REASON_INVALID = "workspace path is not a usable directory"
FORBIDDEN_REASON_OUTSIDE = "path is outside the Accuretta workspace"
FORBIDDEN_REASON_TRAVERSAL = "path traversal rejected"
FORBIDDEN_REASON_BLOCKED = "workspace path is blocked"


@dataclass(frozen=True)
class CodexWorkspaceBinding:
    """Resolved workspace for a Codex thread (safe fields only)."""

    cwd: Optional[str]
    ok: bool
    reason: Optional[str]
    source: str  # chat_bound | accuretta_active | none
    # Coding via Codex native tools is never enabled in this build.
    coding_actions_allowed: bool = False
    mode: str = "advisory_chat_only"

    def to_safe_dict(self) -> dict:
        out = {
            "cwd": self.cwd,
            "ok": self.ok,
            "reason": self.reason,
            "source": self.source,
            "codingActionsAllowed": False,
            "mode": self.mode,
            "displayLabel": self.cwd or "No workspace (chat-only)",
        }
        assert_safe_provider_payload(out)
        return out


def clear_codex_cwd_for_chat(chat_id: str) -> None:
    if not chat_id:
        return
    with _CWD_LOCK:
        _CWD_BY_CHAT.pop(str(chat_id), None)


def bind_codex_cwd(chat_id: str, cwd: str) -> None:
    if not chat_id or not cwd:
        return
    normalized = _normalize(cwd)
    if not normalized:
        return
    with _CWD_LOCK:
        _CWD_BY_CHAT[str(chat_id)] = normalized


def get_bound_codex_cwd(chat_id: str) -> Optional[str]:
    if not chat_id:
        return None
    with _CWD_LOCK:
        return _CWD_BY_CHAT.get(str(chat_id))


def _normalize(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    # Reject obvious traversal tokens before expand (defense in depth).
    if "\x00" in raw:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(raw))
    try:
        return str(Path(expanded).resolve(strict=False))
    except Exception:
        try:
            return os.path.abspath(expanded)
        except Exception:
            return ""


def _is_filesystem_root(path: str) -> bool:
    n = _normalize(path)
    if not n:
        return True
    try:
        p = Path(n)
        anchor = str(Path(p.anchor).resolve(strict=False)) if p.anchor else ""
        if os.name == "nt":
            return n.rstrip("\\/").lower() == anchor.rstrip("\\/").lower() or len(n.rstrip("\\/")) <= 3
        return n == "/"
    except Exception:
        return n == "/"


def _is_user_home(path: str) -> bool:
    try:
        home = str(Path.home().resolve(strict=False))
    except Exception:
        home = os.path.expanduser("~")
    n = _normalize(path)
    if not n or not home:
        return False
    if os.name == "nt":
        return n.lower() == home.lower()
    return n == home


def _accuretta_workspace_folders() -> list[str]:
    """Read Accuretta workspace folders without importing bridge at module load."""
    try:
        import bridge
        ws = bridge.get_workspace()
        folders = ws.get("folders") if isinstance(ws, dict) else []
    except Exception:
        return []
    out: list[str] = []
    for folder in folders or []:
        if not isinstance(folder, str) or not folder.strip():
            continue
        n = _normalize(folder)
        if n:
            out.append(n)
    # De-dupe preserving order
    seen = set()
    unique: list[str] = []
    for item in out:
        key = item.lower() if os.name == "nt" else item
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _is_under_any(path: str, roots: Sequence[str]) -> bool:
    n = _normalize(path)
    if not n:
        return False
    for root in roots:
        r = _normalize(root)
        if not r:
            continue
        try:
            if os.name == "nt":
                common = os.path.commonpath([r.lower(), n.lower()])
                if common == r.lower():
                    return True
            else:
                common = os.path.commonpath([r, n])
                if common == r:
                    return True
        except Exception:
            continue
    return False


def validate_codex_cwd(
    path: str,
    *,
    allowed_roots: Optional[Sequence[str]] = None,
) -> CodexWorkspaceBinding:
    """Validate a candidate cwd against Accuretta workspace roots."""
    roots = list(allowed_roots) if allowed_roots is not None else _accuretta_workspace_folders()
    if not roots:
        return CodexWorkspaceBinding(
            cwd=None, ok=False, reason=FORBIDDEN_REASON_MISSING, source="none"
        )

    raw = (path or "").strip()
    if not raw:
        return CodexWorkspaceBinding(
            cwd=None, ok=False, reason=FORBIDDEN_REASON_MISSING, source="none"
        )

    # Path traversal / empty after normalize
    if ".." in Path(raw).parts:
        # Still allow if resolve lands inside roots — but reject if normalize fails
        # to stay under roots.
        pass

    normalized = _normalize(raw)
    if not normalized:
        return CodexWorkspaceBinding(
            cwd=None, ok=False, reason=FORBIDDEN_REASON_TRAVERSAL, source="none"
        )

    if _is_filesystem_root(normalized):
        return CodexWorkspaceBinding(
            cwd=None, ok=False, reason=FORBIDDEN_REASON_ROOT, source="none"
        )
    if _is_user_home(normalized):
        return CodexWorkspaceBinding(
            cwd=None, ok=False, reason=FORBIDDEN_REASON_HOME, source="none"
        )

    try:
        import bridge
        if bridge.is_blocked_path(normalized):
            return CodexWorkspaceBinding(
                cwd=None, ok=False, reason=FORBIDDEN_REASON_BLOCKED, source="none"
            )
    except Exception:
        pass

    if not _is_under_any(normalized, roots):
        # Exact root match already covered by under-any; reject outsiders.
        return CodexWorkspaceBinding(
            cwd=None, ok=False, reason=FORBIDDEN_REASON_OUTSIDE, source="none"
        )

    try:
        p = Path(normalized)
        if not p.is_dir():
            return CodexWorkspaceBinding(
                cwd=None, ok=False, reason=FORBIDDEN_REASON_INVALID, source="none"
            )
    except Exception:
        return CodexWorkspaceBinding(
            cwd=None, ok=False, reason=FORBIDDEN_REASON_INVALID, source="none"
        )

    return CodexWorkspaceBinding(
        cwd=normalized, ok=True, reason=None, source="validated"
    )


def resolve_codex_workspace(
    *,
    chat_id: Optional[str] = None,
    preferred_cwd: Optional[str] = None,
) -> CodexWorkspaceBinding:
    """Pick the cwd Codex should use for this Accuretta conversation.

    Order:
    1. preferred_cwd (persisted chat binding) if still valid
    2. in-memory bind for chat_id if still valid
    3. first Accuretta workspace folder (active project root)
    """
    roots = _accuretta_workspace_folders()
    if not roots:
        return CodexWorkspaceBinding(
            cwd=None, ok=False, reason=FORBIDDEN_REASON_MISSING, source="none"
        )

    candidates: list[tuple[str, str]] = []
    if isinstance(preferred_cwd, str) and preferred_cwd.strip():
        candidates.append((preferred_cwd.strip(), "chat_bound"))
    if chat_id:
        bound = get_bound_codex_cwd(chat_id)
        if bound:
            candidates.append((bound, "chat_bound"))
    # Active Accuretta project root = first configured folder.
    candidates.append((roots[0], "accuretta_active"))

    seen = set()
    for path, source in candidates:
        key = path.lower() if os.name == "nt" else path
        if key in seen:
            continue
        seen.add(key)
        result = validate_codex_cwd(path, allowed_roots=roots)
        if result.ok and result.cwd:
            return CodexWorkspaceBinding(
                cwd=result.cwd,
                ok=True,
                reason=None,
                source=source,
                coding_actions_allowed=False,
                mode="advisory_chat_only",
            )

    return CodexWorkspaceBinding(
        cwd=None,
        ok=False,
        reason=FORBIDDEN_REASON_MISSING,
        source="none",
        coding_actions_allowed=False,
        mode="advisory_chat_only",
    )


def workspace_status_for_ui(*, chat_id: Optional[str] = None) -> dict:
    binding = resolve_codex_workspace(chat_id=chat_id)
    return binding.to_safe_dict()

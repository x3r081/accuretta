"""Discover the official Codex CLI executable.

Newly written for Accuretta. Never installs Codex and never executes shell aliases.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

ENV_CODEX_BIN = "ACCURETTA_CODEX_BIN"

# Documented minimum for ChatGPT device-code support in app-server.
# Capability probing remains preferred over rigid version gates.
MINIMUM_TESTED_VERSION = "0.144.6"

_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")

_discovery_cache: Optional["CodexDiscovery"] = None


@dataclass(frozen=True)
class CodexDiscovery:
    executable: Optional[str]
    version: Optional[str]
    available: bool
    disabled_reason: Optional[str]
    source: str  # env | path | homebrew | missing | invalid

    def to_safe_dict(self) -> dict:
        return {
            "installed": bool(self.executable),
            "available": bool(self.available),
            "codexVersion": self.version,
            "discoverySource": self.source,
            "disabledReason": self.disabled_reason,
        }


def reset_discovery_cache() -> None:
    global _discovery_cache
    _discovery_cache = None


def discover_codex(*, force_refresh: bool = False, candidates: Optional[Sequence[str]] = None) -> CodexDiscovery:
    global _discovery_cache
    if not force_refresh and _discovery_cache is not None:
        return _discovery_cache
    result = _discover(candidates=candidates)
    _discovery_cache = result
    return result


def _candidate_paths(extra: Optional[Sequence[str]] = None) -> List[str]:
    out: List[str] = []
    if extra:
        out.extend(str(x) for x in extra if x)
    env = (os.environ.get(ENV_CODEX_BIN) or "").strip()
    if env:
        out.append(env)
    which = shutil.which("codex")
    if which:
        out.append(which)
    out.extend([
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ])
    # De-dupe preserving order
    seen = set()
    unique: List[str] = []
    for item in out:
        key = os.path.normpath(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _validate_executable(path: str) -> Optional[str]:
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return None
        if p.is_dir():
            return None
        resolved = p.resolve()
        if not os.access(resolved, os.X_OK):
            return None
        if not resolved.is_file():
            return None
        return str(resolved)
    except Exception:
        return None


def _probe_version(executable: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
            shell=False,
            check=False,
        )
    except Exception:
        return None
    # Prefer stdout only — stderr may contain noisy / sensitive diagnostics.
    text = (proc.stdout or "").strip()
    if not text:
        text = (proc.stderr or "").strip()
        # Never accept Bearer-like stderr as a version string.
        if "bearer " in text.lower() or "authorization:" in text.lower():
            return None
    if not text:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else text.splitlines()[0][:80]


def _discover(*, candidates: Optional[Sequence[str]] = None) -> CodexDiscovery:
    env = (os.environ.get(ENV_CODEX_BIN) or "").strip()
    paths = _candidate_paths(candidates)
    for raw in paths:
        validated = _validate_executable(raw)
        if not validated:
            if env and os.path.normpath(os.path.expanduser(env)) == os.path.normpath(os.path.expanduser(raw)):
                return CodexDiscovery(
                    executable=None,
                    version=None,
                    available=False,
                    disabled_reason="Configured Codex executable is invalid",
                    source="invalid",
                )
            continue
        version = _probe_version(validated)
        if env and os.path.normpath(os.path.expanduser(env)) == os.path.normpath(raw):
            source = "env"
        elif shutil.which("codex") and os.path.normpath(shutil.which("codex") or "") == os.path.normpath(raw):
            source = "path"
        elif raw in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
            source = "homebrew"
        else:
            source = "path"
        return CodexDiscovery(
            executable=validated,
            version=version,
            available=True,
            disabled_reason=None,
            source=source,
        )
    return CodexDiscovery(
        executable=None,
        version=None,
        available=False,
        disabled_reason="Codex CLI not installed",
        source="missing",
    )

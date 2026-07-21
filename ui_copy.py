"""Platform-aware UI copy catalog for Accuretta.

Single source of truth for onboarding / settings strings. The backend merges
neutral defaults with OS overrides and returns them on /api/setup/sysinfo so
the frontend never needs navigator.userAgent (or scattered sys.platform checks)
to pick wording.
"""

from __future__ import annotations

import sys
from typing import Any


def normalize_os(platform_name: str | None = None) -> str:
    """Map sys.platform / hardware 'os' fields to windows | macos | linux."""
    raw = (platform_name or sys.platform or "").strip().lower()
    if raw.startswith("win"):
        return "windows"
    if raw in ("darwin", "macos", "mac", "osx"):
        return "macos"
    if "linux" in raw or raw.startswith("freebsd") or raw.startswith("openbsd"):
        return "linux"
    # Unknown Unix-likes get POSIX-oriented copy (no WSL).
    if raw and not raw.startswith("win"):
        return "linux"
    return "windows" if sys.platform.startswith("win") else (
        "macos" if sys.platform == "darwin" else "linux"
    )


def ui_features(os_key: str) -> dict[str, bool]:
    """Capability flags that gate whole UI sections (not per-string branches)."""
    windows = os_key == "windows"
    macos = os_key == "macos"
    return {
        "sandbox_wsl": windows,
        "llama_one_click_windows": windows,
        "registry_tools": windows,
        "network_snapshot": windows,
        "metal_guidance": macos,
        "homebrew_paths": macos,
    }


# Shared / neutral strings — used on every platform unless overridden.
_NEUTRAL: dict[str, str] = {
    "llama_bin.label": "llama-server executable",
    "llama_bin.basename": "llama-server",
    "models_dir.label": "Models folder",
    "models_dir.hint": "folder containing .gguf files. picked folder is scanned recursively.",
    "setup.subtitle": "Select your llama-server and model to get started.",
    "setup.scanning_title": "Scanning System Specs & Files…",
    "setup.scanning_desc": (
        "Checking hardware and searching for a local llama-server to minimize setup friction."
    ),
    "setup.ready_title": "System Ready: llama-server detected",
    "setup.missing_title": "Action Required: Missing llama-server",
    "setup.models_dir.label": "Models directory",
    "setup.models_dir.hint": "Where your .gguf files live. Leave blank to use the default.",
    "setup.llama_hint": (
        "Auto-detected from PATH and common locations — or set llama_bin in settings."
    ),
    "setup.missing_body_prefix": "We couldn't locate a local llama-server executable. ",
    "workspace.path_placeholder": "/path/to/folder",
    "models_dir.placeholder": "~/models",
    "setup.models_dir.placeholder": "e.g. ~/models",
    "memory_budget.label": "Memory budget",
    "memory_budget.autotune_group": "· pick your memory budget",
    "memory_budget.tooltip": (
        "How much GPU or system memory to plan around. The auto-tuner leaves "
        "headroom for the KV cache."
    ),
    "memory_budget.detecting": "detecting memory…",
    "memory_budget.none": "no GPU memory auto-detected — pick a memory tier manually if you want a suggestion",
    "memory_budget.tier_toast": "pick a memory tier (or leave on Manual to skip auto-tune)",
    "ctx.tooltip": (
        "How many tokens the model can remember at once. Each token ≈ 0.75 words. "
        "Larger = longer conversations but more memory usage."
    ),
    "ctx.hint": "tokens the model sees. Bigger = more memory, slower.",
    "batch.tooltip": (
        "How many tokens processed in parallel. Larger = faster prompt processing but more memory."
    ),
    "kv.tooltip": (
        "q4_0 saves ~50% memory vs q8_0 with tiny quality loss. f16 is best quality but uses "
        "full memory. Changing this restarts llama-server."
    ),
    "kv.opt_q4": "q4_0 · smallest memory",
    "kv.hint": "lower = more context window fits in memory. saving reloads the model.",
    "shell.name": "Shell",
    "shell.history_title": "Shell command history",
    "shell.history_empty": (
        "No shell commands recorded yet.<br><br>"
        "<span style=\"font-size:11px;\">Anything the agent runs via "
        "<code>run_powershell</code> shows up here.</span>"
    ),
    "shell.history_clear": "Clear all shell command history? This can't be undone.",
    "shell.approval_sub": "The model wants to run a shell command on your machine.",
    "shell.approval_info": (
        "Shell commands run with your current user privileges. "
        "Read the command below carefully before approving."
    ),
    "trust.title": (
        "Trust writes — auto-approve saving and editing files in your workspace. "
        "System-protected paths and shell commands still require approval."
    ),
    "trust.toast_on": (
        "Trust writes on — files save and edit without asking. "
        "System-protected paths and shell commands still require approval."
    ),
    "faq.modes": (
        "<b>IDE</b> — model replies with one HTML document and the right pane renders it live, "
        "saving every version. good for building landing pages, mocks, dashboards.<br>"
        "<b>Agent</b> — model calls tools to list files, read, write, and run shell commands. "
        "every write/run asks for your approval first.<br>"
        "<b>Auto</b> — bridge picks IDE vs. Agent based on what you asked."
    ),
    "faq.network": (
        "a one-click prompt that asks the model to scan <i>this</i> machine's active TCP/UDP "
        "connections, listening ports, and recent DNS cache, then flag anything weird. "
        "all data is gathered locally — nothing leaves your machine."
    ),
    "desktop.allowlist.label": "App allowlist — one per line (app name or absolute path)",
    "desktop.allowlist.placeholder": "Terminal\nfirefox\n/usr/bin/firefox",
    "desktop.allowlist.hint": (
        "matches any app/path that contains a line from this list (case-insensitive). "
        "leave empty to block all launches. restart not required."
    ),
    "sandbox.section_hint": "",
    "sandbox.wiz_checking": "Checking sandbox…",
    "sandbox.wiz_desc": (
        "Optional isolated Linux guest for offensive tools and unpacking untrusted files."
    ),
    "sandbox.chip.no_wsl": "not available",
    "sandbox.no_wsl_title": "Sandbox unavailable",
    "sandbox.no_wsl_body": "Sandbox is not available on this platform.",
    "sandbox.remove_confirm": (
        "Removes the sandbox guest and its disk image. You can set it up again anytime."
    ),
}

_WINDOWS: dict[str, str] = {
    "llama_bin.basename": "llama-server.exe",
    "workspace.path_placeholder": r"C:\path\to\folder",
    "models_dir.placeholder": r"C:\Users\you\MODELS",
    "setup.models_dir.placeholder": r"e.g. D:\MODELS",
    "setup.llama_hint": (
        "Auto-detected from PATH and common folders — or set llama_bin in settings "
        "(llama-server.exe)."
    ),
    "setup.missing_install": (
        "Install llama.cpp for your GPU, use the 1-click installer below, or point "
        "settings at llama-server.exe."
    ),
    "memory_budget.autotune_group": "· pick your VRAM",
    "memory_budget.tooltip": (
        "How much GPU VRAM to plan around. NVIDIA: via nvidia-smi when available. "
        "The auto-tuner leaves headroom for the KV cache."
    ),
    "memory_budget.detecting": "detecting GPU…",
    "memory_budget.none": "no GPU VRAM auto-detected — pick a VRAM tier manually if you want a suggestion",
    "memory_budget.tier_toast": "pick a VRAM tier (or leave on Manual to skip auto-tune)",
    "ctx.tooltip": (
        "How many tokens the model can remember at once. Each token ≈ 0.75 words. "
        "Larger = longer conversations but more VRAM usage."
    ),
    "ctx.hint": "tokens the model sees. Bigger = more VRAM, slower.",
    "batch.tooltip": (
        "How many tokens processed in parallel. Larger = faster prompt processing but more VRAM."
    ),
    "kv.tooltip": (
        "q4_0 saves ~50% VRAM vs q8_0 with tiny quality loss. f16 is best quality but uses "
        "full VRAM. Changing this restarts llama-server."
    ),
    "kv.opt_q4": "q4_0 · smallest VRAM",
    "kv.hint": "lower = more context window fits in VRAM. saving reloads the model.",
    "shell.name": "PowerShell",
    "shell.history_title": "PowerShell command history",
    "shell.history_empty": (
        "No PowerShell commands recorded yet.<br><br>"
        "<span style=\"font-size:11px;\">Anything the agent runs via "
        "<code>run_powershell</code> shows up here.</span>"
    ),
    "shell.history_clear": "Clear all PowerShell command history? This can't be undone.",
    "shell.approval_sub": "The model wants to run a PowerShell command on your machine.",
    "shell.approval_info": (
        "PowerShell commands run with your current user privileges. "
        "Read the command below carefully before approving."
    ),
    "trust.title": (
        "Trust writes — auto-approve saving and editing files in your workspace. "
        "Registry edits, Windows system folders, and PowerShell still require approval."
    ),
    "trust.toast_on": (
        "Trust writes on — files save and edit without asking. "
        "Registry, Windows system folders, and PowerShell still require approval."
    ),
    "faq.modes": (
        "<b>IDE</b> — model replies with one HTML document and the right pane renders it live, "
        "saving every version. good for building landing pages, mocks, dashboards.<br>"
        "<b>Agent</b> — model calls tools to list files, read, write, run powershell. "
        "every write/run asks for your approval first.<br>"
        "<b>Auto</b> — bridge picks IDE vs. Agent based on what you asked."
    ),
    "faq.network": (
        "a one-click prompt under the network button that asks the model to scan <i>this</i> "
        "machine's active TCP/UDP connections, listening ports, and recent DNS cache, then "
        "flag anything weird (unknown processes phoning home, suspicious remote IPs, etc.). "
        "a chart card renders inline showing connection counts, top processes, and top remote "
        "endpoints — favicons fetched from DuckDuckGo. all data is gathered locally via "
        "PowerShell — nothing leaves your machine."
    ),
    "desktop.allowlist.label": "App allowlist — one per line (exe name or absolute path)",
    "desktop.allowlist.placeholder": (
        "notepad\nchrome\n"
        r"C:\Program Files\Electronic Arts\EA Desktop\EA Desktop\EADesktop.exe"
    ),
    "desktop.allowlist.hint": (
        "matches any exe/path that contains a line from this list (case-insensitive). "
        "leave empty to block all launches. restart not required."
    ),
    "sandbox.section_hint": (
        "an isolated Ubuntu guest (<code>accuretta-sbx</code>, via WSL2) for running offensive "
        "tools and unpacking untrusted files off your host — the workspace is visible inside "
        "at <code>/mnt/…</code>. optional: in a web red-team the exploits run on the target, "
        "not here, so this matters mainly when you pull loot back to analyze locally. "
        "first-time set-up with full progress lives in the <b>Setup Wizard</b>."
    ),
    "sandbox.wiz_checking": "Checking for WSL…",
    "sandbox.wiz_desc": (
        "Runs offensive tools and unpacks untrusted files in an isolated Ubuntu guest "
        "(via WSL2) so nothing touches your host. Your workspace is visible inside it."
    ),
    "sandbox.chip.no_wsl": "WSL not installed",
    "sandbox.no_wsl_title": "WSL not installed (one-time)",
    "sandbox.no_wsl_body": (
        "The sandbox needs WSL2. In an <strong>elevated PowerShell</strong> run "
        "<code>wsl --install</code>, reboot once, then reopen this wizard — everything "
        "after is automatic. This step is optional; you can start using accuretta without it."
    ),
    "sandbox.remove_confirm": (
        "Unregisters the accuretta-sbx WSL distro and deletes its disk image. "
        "You can set it up again anytime."
    ),
}

_MACOS: dict[str, str] = {
    "llama_bin.basename": "llama-server",
    "workspace.path_placeholder": "/Users/you/Projects/my-app",
    "models_dir.placeholder": "/Users/you/models",
    "setup.models_dir.placeholder": "e.g. /Users/you/models",
    "setup.llama_hint": (
        "Auto-detected from PATH and Homebrew (/opt/homebrew/bin, /usr/local/bin) "
        "— or set llama_bin in settings."
    ),
    "setup.missing_install": (
        "On macOS install a Metal build with Homebrew: "
        "<code>brew install llama.cpp</code> — then reopen this wizard."
    ),
    "memory_budget.tooltip": (
        "How much usable unified memory to plan around (total RAM minus a reserve for "
        "macOS and apps). Metal accelerates when your llama.cpp build exposes MTL devices. "
        "The auto-tuner leaves additional headroom for the KV cache."
    ),
    "memory_budget.detecting": "detecting unified memory…",
    "memory_budget.none": (
        "couldn't read unified memory — pick a memory tier manually if you want a suggestion"
    ),
    "desktop.allowlist.placeholder": (
        "Safari\nTerminal\n/Applications/Safari.app"
    ),
    "desktop.allowlist.label": "App allowlist — one per line (app name or absolute path)",
}

_LINUX: dict[str, str] = {
    "llama_bin.basename": "llama-server",
    "workspace.path_placeholder": "/home/you/projects/my-app",
    "models_dir.placeholder": "/home/you/models",
    "setup.models_dir.placeholder": "e.g. /home/you/models",
    "setup.llama_hint": (
        "Auto-detected from PATH and common prefixes — or set llama_bin in settings."
    ),
    "setup.missing_install": (
        "Install llama.cpp (distro package or build from source), ensure "
        "<code>llama-server</code> is on your PATH, or set <code>llama_bin</code>."
    ),
    "memory_budget.tooltip": (
        "How much GPU VRAM (or system memory) to plan around when a discrete GPU is "
        "available. The auto-tuner leaves headroom for the KV cache."
    ),
    "desktop.allowlist.placeholder": "firefox\ngnome-terminal\n/usr/bin/firefox",
}

_OS_OVERRIDES: dict[str, dict[str, str]] = {
    "windows": _WINDOWS,
    "macos": _MACOS,
    "linux": _LINUX,
}


def resolve_ui_copy(os_key: str | None = None) -> dict[str, str]:
    """Deep-merge neutral + OS overrides into a flat key → string map."""
    key = normalize_os(os_key)
    out = dict(_NEUTRAL)
    out.update(_OS_OVERRIDES.get(key, {}))
    return out


def build_ui_capabilities(
    *,
    os_name: str | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Return {os, features, copy} for attachment to sysinfo / dedicated APIs."""
    os_key = normalize_os(os_name or platform_name)
    return {
        "os": os_key,
        "features": ui_features(os_key),
        "copy": resolve_ui_copy(os_key),
    }


def attach_ui_capabilities(info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge capabilities into a hardware/sysinfo dict (mutates and returns it).

    When hardware detection omits ``os``, falls back to ``sys.platform`` via
    ``normalize_os`` so feature flags still resolve (Windows fail-open path).
    """
    base = dict(info or {})
    # Prefer explicit os from hardware when present; else host platform.
    caps = build_ui_capabilities(
        os_name=str(base["os"]) if base.get("os") else None,
        platform_name=None,
    )
    base["os"] = caps["os"]
    base["features"] = caps["features"]
    base["copy"] = caps["copy"]
    return base


# Keys that must never appear in macOS/Linux copy values (regression guard).
_WINDOWS_ONLY_MARKERS = (
    "elevated PowerShell",
    "wsl --install",
    "WSL not installed",
    "WSL2",
    "llama-server.exe",
    r"C:\path\to\folder",
    r"C:\Users\you\MODELS",
    r"D:\MODELS",
)


def assert_platform_copy_sanitized(os_key: str, copy: dict[str, str] | None = None) -> None:
    """Raise AssertionError if platform copy leaks another OS's guidance.

    Used by unit tests (and optional CI) for deterministic rendering checks.
    """
    key = normalize_os(os_key)
    c = copy if copy is not None else resolve_ui_copy(key)
    feats = ui_features(key)

    if key != "windows":
        assert feats["sandbox_wsl"] is False
        assert feats["llama_one_click_windows"] is False
        assert feats["registry_tools"] is False
        assert feats["network_snapshot"] is False
        blob = "\n".join(c.values())
        for marker in _WINDOWS_ONLY_MARKERS:
            if marker in blob:
                raise AssertionError(f"{key} copy must not contain {marker!r}")
        # Placeholders must be POSIX-ish
        for pk in (
            "workspace.path_placeholder",
            "models_dir.placeholder",
            "setup.models_dir.placeholder",
        ):
            ph = c.get(pk, "")
            if "\\" in ph or ph.upper().startswith("C:"):
                raise AssertionError(f"{key} placeholder {pk}={ph!r} is not POSIX")

    if key == "windows":
        assert feats["sandbox_wsl"] is True
        assert "WSL" in c.get("sandbox.wiz_checking", "") or "WSL" in c.get(
            "sandbox.chip.no_wsl", ""
        )
        assert c["llama_bin.basename"] == "llama-server.exe"
        assert "C:\\" in c["workspace.path_placeholder"] or c[
            "workspace.path_placeholder"
        ].startswith("C:")

    if key == "macos":
        assert feats["metal_guidance"] is True
        assert feats["homebrew_paths"] is True
        assert "Homebrew" in c["setup.llama_hint"] or "brew" in c.get(
            "setup.missing_install", ""
        ).lower()
        assert "/Users/" in c["workspace.path_placeholder"]
        assert c["llama_bin.basename"] == "llama-server"

    if key == "linux":
        assert feats["metal_guidance"] is False
        assert "/home/" in c["workspace.path_placeholder"] or c[
            "workspace.path_placeholder"
        ].startswith("/")
        assert "WSL" not in c.get("sandbox.section_hint", "")
        assert c["llama_bin.basename"] == "llama-server"

"""Launcher readiness probes — Accuretta identity, not bare TCP.

Newly written for Accuretta. Used by the desktop launcher so an unrelated
process listening on the bridge port is never treated as Accuretta.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Optional

# Must match the ``app`` field returned by ``GET /api/health``.
ACCURETTA_APP_ID = "accuretta"


def port_accepts_tcp(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_health_json(
    host: str,
    port: int,
    *,
    timeout: float = 1.5,
) -> Optional[dict[str, Any]]:
    url = f"http://{host}:{port}/api/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read().decode("utf-8", errors="replace") or "{}"
            data = json.loads(raw)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_accuretta_health_payload(payload: Any) -> bool:
    """True when the JSON body is Accuretta's health marker."""
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is not True:
        return False
    return payload.get("app") == ACCURETTA_APP_ID


def probe_accuretta(
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    timeout: float = 1.5,
) -> bool:
    """Return True only if Accuretta answers ``GET /api/health`` with its marker."""
    return is_accuretta_health_payload(fetch_health_json(host, port, timeout=timeout))


def wait_for_accuretta(
    host: str = "127.0.0.1",
    port: int = 8787,
    *,
    timeout: float = 40.0,
    poll_s: float = 0.25,
) -> bool:
    """Poll until Accuretta health succeeds or ``timeout`` elapses."""
    deadline = time.time() + max(0.1, float(timeout))
    while time.time() < deadline:
        if probe_accuretta(host, port, timeout=min(1.5, poll_s * 4)):
            return True
        time.sleep(poll_s)
    return False

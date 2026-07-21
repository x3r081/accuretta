"""Cross-process advisory file locking.

Adapted from Nous Research Hermes Agent (MIT), hermes_cli/auth.py
``_file_lock`` / ``_auth_store_lock`` patterns. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore

_lock_holders: dict[str, threading.local] = {}
_lock_holders_guard = threading.Lock()


def _holder_for(lock_path: Path) -> threading.local:
    key = str(lock_path.resolve()) if lock_path.exists() else str(lock_path)
    with _lock_holders_guard:
        holder = _lock_holders.get(key)
        if holder is None:
            holder = threading.local()
            _lock_holders[key] = holder
        return holder


@contextmanager
def file_lock(
    lock_path: Path,
    *,
    timeout_seconds: float = 15.0,
    timeout_message: str = "Timed out waiting for credential store lock",
) -> Iterator[None]:
    """Cross-process advisory flock; reentrant per-thread."""
    holder = _holder_for(lock_path)
    if getattr(holder, "depth", 0) > 0:
        holder.depth += 1
        try:
            yield
        finally:
            holder.depth -= 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    with lock_path.open("r+" if msvcrt else "a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while True:
            try:
                if fcntl:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(timeout_message)
                time.sleep(0.05)

        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
            if fcntl:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass

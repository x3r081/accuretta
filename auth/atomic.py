"""Atomic secret-file writes with owner-only permissions.

Adapted from Nous Research Hermes Agent (MIT) patterns in
hermes_cli/auth.py ``_save_auth_store`` and utils.py ``atomic_replace``.
See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Union


def secure_parent_dir(path: Path) -> None:
    """Ensure parent directory exists with owner-only permissions when possible."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        # Refuse to chmod filesystem roots / top-level dirs.
        if parent.resolve() in (Path("/"), Path(parent.anchor)):
            return
        if len(parent.resolve().parts) <= 2:
            return
        parent.chmod(stat.S_IRWXU)
    except OSError:
        pass


def atomic_replace(tmp_path: Union[str, Path], target: Union[str, Path]) -> str:
    """Atomically move tmp onto target, preserving symlink destinations."""
    target_str = str(target)
    real_path = os.path.realpath(target_str) if os.path.islink(target_str) else target_str
    tmp_str = str(tmp_path)
    try:
        os.replace(tmp_str, real_path)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EBUSY):
            raise
        import shutil

        shutil.copyfile(tmp_str, real_path)
        try:
            with open(real_path, "rb") as handle:
                os.fsync(handle.fileno())
        except OSError:
            pass
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
    return real_path


def atomic_write_secrets(path: Path, payload: Any) -> None:
    """Write JSON secrets with O_EXCL 0o600, fsync, and atomic replace."""
    path = Path(path)
    secure_parent_dir(path)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

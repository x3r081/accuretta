"""Secure file-based credential fallback store.

Adapted from Nous Research Hermes Agent (MIT) auth store persistence
patterns (atomic 0o600 writes + flock). See THIRD_PARTY_NOTICES.md.

Newly written Accuretta layout: per-provider entries under a single JSON
file owned by the current user. Never commit this file to Git.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from .atomic import atomic_write_secrets, secure_parent_dir
from .lock import file_lock
from .models import StoredCredential

log = logging.getLogger("accuretta.auth.file_store")

AUTH_STORE_VERSION = 1
DEFAULT_FILENAME = "credentials.json"


def default_credentials_path() -> Path:
    """Resolve the secure fallback credential file path."""
    # Prefer XDG-ish local data under the Accuretta data dir when available,
    # otherwise ~/.accuretta/credentials.json.
    try:
        from pathlib import Path as _P
        import os

        # Match bridge DATA location without importing bridge (circular-safe).
        env = (os.environ.get("ACCURETTA_DATA") or "").strip()
        if env:
            return _P(env).expanduser().resolve() / "auth" / DEFAULT_FILENAME
        repo_data = _P(__file__).resolve().parent.parent / "data" / "auth" / DEFAULT_FILENAME
        # Always use repo data/auth when running from the Accuretta tree.
        return repo_data
    except Exception:
        return Path.home() / ".accuretta" / DEFAULT_FILENAME


class FileAuthStore:
    """Owner-only JSON credential store with locking and atomic writes."""

    source_name = "file"

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else default_credentials_path()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _empty(self) -> dict:
        return {"version": AUTH_STORE_VERSION, "providers": {}}

    def _load_unlocked(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            corrupt = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                shutil.copy2(self.path, corrupt)
            except Exception:
                pass
            log.warning(
                "auth file store unreadable (%s); starting empty. corrupt copy=%s",
                type(exc).__name__,
                corrupt,
            )
            return self._empty()
        if not isinstance(raw, dict):
            return self._empty()
        providers = raw.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        return {"version": AUTH_STORE_VERSION, "providers": providers}

    def _save_unlocked(self, store: dict) -> None:
        secure_parent_dir(self.path)
        payload = {
            "version": AUTH_STORE_VERSION,
            "providers": store.get("providers") or {},
        }
        atomic_write_secrets(self.path, payload)

    def load(self, provider_id: str) -> Optional[StoredCredential]:
        with file_lock(self.lock_path):
            store = self._load_unlocked()
            raw = store["providers"].get(provider_id)
            if raw is None:
                return None
            try:
                return StoredCredential.from_storage_dict(
                    raw, provider_id=provider_id, source=self.source_name
                )
            except ValueError:
                log.warning(
                    "auth file store: malformed credential for provider_id=%s; ignoring",
                    provider_id,
                )
                return None

    def save(self, provider_id: str, credential: StoredCredential) -> None:
        if credential.provider_id != provider_id:
            raise ValueError("provider_id mismatch")
        with file_lock(self.lock_path):
            store = self._load_unlocked()
            store["providers"][provider_id] = credential.to_storage_dict()
            self._save_unlocked(store)

    def delete(self, provider_id: str) -> bool:
        with file_lock(self.lock_path):
            store = self._load_unlocked()
            existed = provider_id in store["providers"]
            if existed:
                del store["providers"][provider_id]
                self._save_unlocked(store)
            return existed

    def list_authenticated_providers(self) -> List[str]:
        with file_lock(self.lock_path):
            store = self._load_unlocked()
            return sorted(store["providers"].keys())

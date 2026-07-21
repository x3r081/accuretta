"""AuthStore interface and factory.

Newly written for Accuretta. On macOS prefer Keychain via keyring; use a
secure file store when Keychain/keyring is unavailable. Never fail app
startup solely because keyring is missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol, runtime_checkable

from .file_store import FileAuthStore
from .keychain_store import KeychainAuthStore, keyring_available, prefer_keychain
from .models import StoredCredential

log = logging.getLogger("accuretta.auth.store")


@runtime_checkable
class AuthStore(Protocol):
    source_name: str

    def load(self, provider_id: str) -> Optional[StoredCredential]: ...

    def save(self, provider_id: str, credential: StoredCredential) -> None: ...

    def delete(self, provider_id: str) -> bool: ...

    def list_authenticated_providers(self) -> List[str]: ...


@dataclass
class AuthStoreInfo:
    store: AuthStore
    backend: str  # keychain | file
    secure_cloud_auth_available: bool
    detail: str


class CompositeAuthStore:
    """Primary store with optional secondary delete/list mirroring.

    Writes go to the primary backend. Deletes attempt both so logout clears
    credentials regardless of which backend last wrote.
    """

    def __init__(self, primary: AuthStore, fallback: Optional[AuthStore] = None):
        self.primary = primary
        self.fallback = fallback
        self.source_name = primary.source_name

    def load(self, provider_id: str) -> Optional[StoredCredential]:
        cred = self.primary.load(provider_id)
        if cred is not None:
            return cred
        if self.fallback is not None:
            return self.fallback.load(provider_id)
        return None

    def save(self, provider_id: str, credential: StoredCredential) -> None:
        self.primary.save(provider_id, credential)
        # Avoid leaving stale copies in the other backend after a backend switch.
        if self.fallback is not None and self.fallback is not self.primary:
            try:
                self.fallback.delete(provider_id)
            except Exception:
                pass

    def delete(self, provider_id: str) -> bool:
        deleted = False
        try:
            deleted = self.primary.delete(provider_id) or deleted
        except Exception:
            pass
        if self.fallback is not None:
            try:
                deleted = self.fallback.delete(provider_id) or deleted
            except Exception:
                pass
        return deleted

    def list_authenticated_providers(self) -> List[str]:
        ids = set(self.primary.list_authenticated_providers())
        if self.fallback is not None:
            ids.update(self.fallback.list_authenticated_providers())
        return sorted(ids)


def create_auth_store(
    *,
    file_path: Optional[Path] = None,
    force_file: bool = False,
) -> AuthStoreInfo:
    """Create the preferred AuthStore for this platform.

    Returns info describing which backend was selected. Callers may surface
    ``secure_cloud_auth_available`` / ``detail`` in UI without raising.
    """
    file_store = FileAuthStore(path=file_path)

    if force_file or not prefer_keychain():
        detail = (
            "Using secure file credential store"
            if force_file or not keyring_available()
            else "Keychain not preferred on this platform; using secure file store"
        )
        if not keyring_available() and not force_file:
            detail = (
                "Python keyring unavailable; using owner-only file credential store. "
                "Secure cloud authentication storage is limited to the file fallback."
            )
        return AuthStoreInfo(
            store=file_store,
            backend="file",
            secure_cloud_auth_available=True,
            detail=detail,
        )

    try:
        keychain = KeychainAuthStore()
        return AuthStoreInfo(
            store=CompositeAuthStore(primary=keychain, fallback=file_store),
            backend="keychain",
            secure_cloud_auth_available=True,
            detail="Using macOS Keychain via keyring (file store as logout fallback)",
        )
    except Exception as exc:
        log.warning("keychain store unavailable (%s); using file fallback", type(exc).__name__)
        return AuthStoreInfo(
            store=file_store,
            backend="file",
            secure_cloud_auth_available=True,
            detail=(
                "Keychain initialization failed; using owner-only file credential store. "
                f"Reason: {type(exc).__name__}"
            ),
        )

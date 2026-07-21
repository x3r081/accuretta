"""macOS Keychain credential store via the keyring package.

Newly written for Accuretta. Prefer Keychain on macOS; callers fall back to
FileAuthStore when keyring is unavailable. Application startup must not fail
solely because keyring is missing.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import List, Optional

from .models import StoredCredential

log = logging.getLogger("accuretta.auth.keychain")

SERVICE_NAME = "Accuretta"
INDEX_ACCOUNT = "_accuretta_provider_index"


def keyring_available() -> bool:
    try:
        import keyring  # noqa: F401
        return True
    except Exception:
        return False


def _account_for(provider_id: str) -> str:
    return f"provider:{provider_id}"


class KeychainAuthStore:
    """Store credentials in the login Keychain (or platform keyring backend)."""

    source_name = "keychain"

    def __init__(self, service_name: str = SERVICE_NAME):
        self.service_name = service_name
        import keyring

        self._keyring = keyring

    def _load_index(self) -> List[str]:
        raw = self._keyring.get_password(self.service_name, INDEX_ACCOUNT)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [str(x) for x in data if isinstance(x, str)]

    def _save_index(self, providers: List[str]) -> None:
        unique = sorted(set(providers))
        self._keyring.set_password(
            self.service_name,
            INDEX_ACCOUNT,
            json.dumps(unique),
        )

    def load(self, provider_id: str) -> Optional[StoredCredential]:
        raw = self._keyring.get_password(self.service_name, _account_for(provider_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return StoredCredential.from_storage_dict(
                data, provider_id=provider_id, source=self.source_name
            )
        except Exception:
            log.warning(
                "keychain: malformed credential for provider_id=%s; ignoring",
                provider_id,
            )
            return None

    def save(self, provider_id: str, credential: StoredCredential) -> None:
        if credential.provider_id != provider_id:
            raise ValueError("provider_id mismatch")
        payload = json.dumps(credential.to_storage_dict(), sort_keys=True)
        self._keyring.set_password(
            self.service_name,
            _account_for(provider_id),
            payload,
        )
        index = self._load_index()
        if provider_id not in index:
            index.append(provider_id)
            self._save_index(index)

    def delete(self, provider_id: str) -> bool:
        account = _account_for(provider_id)
        existed = self._keyring.get_password(self.service_name, account) is not None
        try:
            self._keyring.delete_password(self.service_name, account)
        except Exception:
            # keyring raises PasswordDeleteError when missing
            existed = False
        index = [p for p in self._load_index() if p != provider_id]
        self._save_index(index)
        return existed

    def list_authenticated_providers(self) -> List[str]:
        return self._load_index()


def prefer_keychain() -> bool:
    return sys.platform == "darwin" and keyring_available()

"""Credential data models.

Newly written for Accuretta. Avoid storing unnecessary identity/profile data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

CREDENTIAL_SCHEMA_VERSION = 1


@dataclass
class StoredCredential:
    """Persisted provider credential (Keychain or secure file fallback)."""

    provider_id: str
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None  # unix epoch seconds
    token_type: str = "Bearer"
    scopes: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = CREDENTIAL_SCHEMA_VERSION
    # Runtime-only; set by the store backend that loaded the credential.
    source: str = "unknown"

    def __repr__(self) -> str:
        # Never include token material in repr / tracebacks.
        meta_keys = sorted(self.metadata.keys()) if isinstance(self.metadata, dict) else []
        return (
            f"StoredCredential(provider_id={self.provider_id!r}, "
            f"token_type={self.token_type!r}, "
            f"has_access_token={bool(self.access_token)}, "
            f"has_refresh_token={bool(self.refresh_token)}, "
            f"expires_at={self.expires_at!r}, "
            f"scopes={list(self.scopes)!r}, "
            f"metadata_keys={meta_keys!r}, "
            f"schema_version={self.schema_version}, "
            f"source={self.source!r})"
        )

    def to_storage_dict(self) -> Dict[str, Any]:
        """Serialize for persistence. Does not include the runtime `source`."""
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scopes": list(self.scopes),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_storage_dict(
        cls,
        data: Any,
        *,
        provider_id: Optional[str] = None,
        source: str = "unknown",
    ) -> "StoredCredential":
        if not isinstance(data, dict):
            raise ValueError("credential payload must be an object")
        pid = provider_id or data.get("provider_id")
        if not isinstance(pid, str) or not pid.strip():
            raise ValueError("provider_id is required")
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise ValueError("access_token is required")

        expires_at = data.get("expires_at")
        if expires_at is not None:
            try:
                expires_at = float(expires_at)
            except (TypeError, ValueError) as exc:
                raise ValueError("expires_at must be a number") from exc

        scopes = data.get("scopes") or []
        if not isinstance(scopes, list):
            raise ValueError("scopes must be a list")
        scopes = [str(s) for s in scopes]

        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")

        schema_version = data.get("schema_version", CREDENTIAL_SCHEMA_VERSION)
        try:
            schema_version = int(schema_version)
        except (TypeError, ValueError) as exc:
            raise ValueError("schema_version must be an int") from exc

        refresh = data.get("refresh_token")
        if refresh is not None and not isinstance(refresh, str):
            raise ValueError("refresh_token must be a string or null")

        token_type = data.get("token_type") or "Bearer"
        if not isinstance(token_type, str):
            raise ValueError("token_type must be a string")

        return cls(
            provider_id=pid.strip(),
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
            token_type=token_type,
            scopes=scopes,
            metadata=dict(metadata),
            schema_version=schema_version,
            source=source,
        )

    def safe_public_view(self) -> Dict[str, Any]:
        """Non-sensitive fields suitable for status APIs."""
        label = self.metadata.get("account_label")
        if label is not None:
            label = str(label)
        return {
            "providerId": self.provider_id,
            "authenticated": True,
            "expiresAt": self.expires_at,
            "tokenType": self.token_type,
            "scopes": list(self.scopes),
            "accountLabel": label,
            "schemaVersion": self.schema_version,
            "source": self.source,
        }

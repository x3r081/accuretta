"""Authentication and credential storage for Accuretta.

Newly written Accuretta package. Generic OAuth primitives in submodules are
adapted from patterns in Nous Research Hermes Agent (MIT); see
THIRD_PARTY_NOTICES.md and per-file attribution comments.
"""

from .models import StoredCredential
from .store import AuthStore, create_auth_store

__all__ = [
    "AuthStore",
    "StoredCredential",
    "create_auth_store",
]

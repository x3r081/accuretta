"""PKCE helpers (S256) and OAuth state generation.

Adapted from Nous Research Hermes Agent (MIT) PKCE helpers
(hermes_cli/auth.py ``_oauth_pkce_*`` and Honcho ``_pkce``).
See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass


def generate_code_verifier(length: int = 64) -> str:
    """RFC 7636 code_verifier (43–128 chars of unreserved URL-safe text)."""
    length = max(43, min(128, int(length)))
    raw = base64.urlsafe_b64encode(secrets.token_bytes(length)).decode("ascii")
    return raw.rstrip("=")[:128]


def generate_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def generate_state(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str
    method: str = "S256"


def pkce_pair(length: int = 64) -> PkcePair:
    verifier = generate_code_verifier(length)
    return PkcePair(verifier=verifier, challenge=generate_code_challenge(verifier))

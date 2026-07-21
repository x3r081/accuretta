"""Sanitized, user-readable provider and authentication errors.

Newly written for Accuretta. Keep detailed provider payloads out of
user-visible messages and normal logs.
"""

from __future__ import annotations

from typing import Optional


class ProviderError(Exception):
    """Base class for provider / auth failures exposed to callers."""

    code: str = "provider_error"

    def __init__(self, message: str, *, provider_id: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.provider_id = provider_id

    def to_safe_dict(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.provider_id:
            out["providerId"] = self.provider_id
        return out


class ProviderNotConfigured(ProviderError):
    code = "provider_not_configured"


class AuthenticationRequired(ProviderError):
    code = "authentication_required"


class AuthenticationExpired(ProviderError):
    code = "authentication_expired"


class AuthenticationCancelled(ProviderError):
    code = "authentication_cancelled"


class OAuthStateMismatch(ProviderError):
    code = "oauth_state_mismatch"


class TokenExchangeFailed(ProviderError):
    code = "token_exchange_failed"


class TokenRefreshFailed(ProviderError):
    code = "token_refresh_failed"


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"


class RateLimited(ProviderError):
    code = "rate_limited"


class InvalidProviderResponse(ProviderError):
    code = "invalid_provider_response"

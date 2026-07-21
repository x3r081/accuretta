"""Strict, allowlisted Codex protocol models.

Newly written for Accuretta. Unknown additive fields are tolerated and discarded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from auth.redact import redact_sensitive_text

FORBIDDEN_ACCOUNT_KEYS = frozenset({
    "accesstoken",
    "access_token",
    "refreshtoken",
    "refresh_token",
    "idtoken",
    "id_token",
    "apikey",
    "api_key",
    "authorization",
    "bearer",
    "jwt",
    "chatgptauthtokens",
})

_ALLOWED_AUTH_URL_HOSTS = frozenset({
    "chatgpt.com",
    "www.chatgpt.com",
    "auth.openai.com",
    "openai.com",
    "www.openai.com",
})


@dataclass
class SafeAccountView:
    authenticated: bool = False
    auth_mode: Optional[str] = None
    plan_type: Optional[str] = None
    account_label: Optional[str] = None
    account_type: Optional[str] = None
    requires_openai_auth: Optional[bool] = None

    def to_dict(self) -> dict:
        return {
            "authenticated": self.authenticated,
            "authMode": self.auth_mode,
            "planType": self.plan_type,
            "accountLabel": self.account_label,
            "accountType": self.account_type,
            "requiresOpenaiAuth": self.requires_openai_auth,
        }


@dataclass
class PendingLogin:
    login_id: str
    method: str  # browser | device
    status: str = "pending"
    auth_url: Optional[str] = None
    verification_url: Optional[str] = None
    user_code: Optional[str] = None
    error: Optional[str] = None
    started_at: float = 0.0

    def to_safe_dict(self) -> dict:
        out = {
            "loginId": self.login_id,
            "loginMethod": self.method,
            "loginStatus": self.status,
        }
        if self.auth_url:
            out["authUrl"] = self.auth_url
        if self.verification_url:
            out["verificationUrl"] = self.verification_url
        if self.user_code:
            out["userCode"] = self.user_code
        if self.error:
            out["error"] = self.error
        return out


def sanitize_error_message(message: Any, *, limit: int = 240) -> str:
    text = redact_sensitive_text(str(message or "").strip())
    lowered = text.lower()
    for needle in ("access_token", "refresh_token", "bearer ", "authorization:"):
        if needle in lowered:
            return "Codex request failed"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text or "Codex request failed"


def validate_auth_url(url: str) -> Optional[str]:
    """Return sanitized HTTPS URL if host is expected; strip sensitive query keys."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    if parsed.scheme != "https":
        return None
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_AUTH_URL_HOSTS and not host.endswith(".openai.com"):
        return None
    # Drop sensitive query parameters before exposing to the UI.
    from urllib.parse import parse_qsl, urlencode, urlunparse
    safe_q = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        kl = key.lower()
        if kl in {
            "access_token", "refresh_token", "id_token", "token", "code",
            "client_secret", "authorization",
        }:
            continue
        safe_q.append((key, value))
    cleaned = parsed._replace(query=urlencode(safe_q), fragment="")
    return urlunparse(cleaned)

def validate_verification_url(url: str) -> Optional[str]:
    return validate_auth_url(url)


_USER_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{3,32}$", re.IGNORECASE)


def validate_user_code(code: Any) -> Optional[str]:
    if not isinstance(code, str):
        return None
    text = code.strip()
    if not text or not _USER_CODE_RE.match(text):
        return None
    return text


def parse_account_read_result(payload: Any) -> SafeAccountView:
    if not isinstance(payload, dict):
        return SafeAccountView()
    account = payload.get("account")
    requires = payload.get("requiresOpenaiAuth")
    if requires is not None:
        requires = bool(requires)
    if account is None:
        return SafeAccountView(authenticated=False, requires_openai_auth=requires)
    if not isinstance(account, dict):
        return SafeAccountView(authenticated=False, requires_openai_auth=requires)
    # Discard forbidden keys by never reading them.
    acct_type = account.get("type")
    if not isinstance(acct_type, str):
        acct_type = None
    plan = account.get("planType") or account.get("plan_type")
    if not isinstance(plan, str):
        plan = None
    email = account.get("email")
    label = None
    if isinstance(email, str) and "@" in email and len(email) < 200:
        # Soft label only — never a token.
        label = email.strip()
    auth_mode = None
    if acct_type == "chatgpt":
        auth_mode = "chatgpt"
    elif acct_type == "apiKey":
        auth_mode = "apikey"
    elif isinstance(acct_type, str):
        auth_mode = acct_type
    authenticated = acct_type is not None
    return SafeAccountView(
        authenticated=authenticated,
        auth_mode=auth_mode,
        plan_type=plan,
        account_label=label,
        account_type=acct_type,
        requires_openai_auth=requires,
    )


def parse_login_start_result(payload: Any, *, expected_type: str) -> PendingLogin:
    if not isinstance(payload, dict):
        raise ValueError("Invalid login start response")
    login_id = payload.get("loginId") or payload.get("login_id")
    if not isinstance(login_id, str) or not login_id.strip():
        raise ValueError("loginId missing")
    login_id = login_id.strip()
    typ = str(payload.get("type") or expected_type)
    if expected_type == "chatgpt" or typ == "chatgpt":
        auth_url = validate_auth_url(str(payload.get("authUrl") or payload.get("auth_url") or ""))
        if not auth_url:
            raise ValueError("authUrl missing or rejected")
        return PendingLogin(login_id=login_id, method="browser", auth_url=auth_url, status="pending")
    if expected_type == "chatgptDeviceCode" or typ == "chatgptDeviceCode":
        vurl = validate_verification_url(
            str(payload.get("verificationUrl") or payload.get("verification_url") or "")
        )
        code = validate_user_code(payload.get("userCode") or payload.get("user_code"))
        if not vurl or not code:
            raise ValueError("device login fields missing or rejected")
        return PendingLogin(
            login_id=login_id,
            method="device",
            verification_url=vurl,
            user_code=code,
            status="pending",
        )
    raise ValueError("Unsupported login type")


def parse_login_completed(params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    login_id = params.get("loginId") or params.get("login_id")
    success = bool(params.get("success"))
    error = params.get("error")
    err_text = None
    if error is not None:
        if isinstance(error, dict):
            err_text = sanitize_error_message(error.get("message") or error.get("code") or "login failed")
        else:
            err_text = sanitize_error_message(error)
    return {
        "loginId": str(login_id).strip() if isinstance(login_id, str) else None,
        "success": success,
        "error": err_text,
    }


def parse_account_updated(params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    auth_mode = params.get("authMode") or params.get("auth_mode")
    plan = params.get("planType") or params.get("plan_type")
    return {
        "authMode": str(auth_mode) if isinstance(auth_mode, str) else None,
        "planType": str(plan) if isinstance(plan, str) else None,
    }


def contains_forbidden_keys(obj: Any) -> bool:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).lower().replace("-", "") in FORBIDDEN_ACCOUNT_KEYS or str(key).lower() in FORBIDDEN_ACCOUNT_KEYS:
                return True
            # also check camelCase variants already covered by lower
            if contains_forbidden_keys(value):
                return True
    elif isinstance(obj, list):
        return any(contains_forbidden_keys(x) for x in obj)
    return False

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


def _safe_id(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip() and len(value.strip()) < 200:
        return value.strip()
    return None


def parse_thread_start_result(payload: Any) -> Dict[str, Any]:
    """Extract allowlisted fields from thread/start or thread/resume results."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid thread response")
    thread = payload.get("thread")
    if not isinstance(thread, dict):
        raise ValueError("thread missing")
    thread_id = _safe_id(thread.get("id"))
    if not thread_id:
        raise ValueError("thread.id missing")
    model = payload.get("model")
    if not isinstance(model, str):
        model = thread.get("model") if isinstance(thread.get("model"), str) else None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        cwd = thread.get("cwd") if isinstance(thread.get("cwd"), str) else None
    return {
        "threadId": thread_id,
        "model": model,
        "cwd": cwd,
        "modelProvider": payload.get("modelProvider")
        if isinstance(payload.get("modelProvider"), str)
        else None,
    }


def parse_turn_start_result(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Invalid turn/start response")
    turn = payload.get("turn")
    if not isinstance(turn, dict):
        raise ValueError("turn missing")
    turn_id = _safe_id(turn.get("id"))
    if not turn_id:
        raise ValueError("turn.id missing")
    status = turn.get("status")
    if not isinstance(status, str):
        status = "inProgress"
    return {"turnId": turn_id, "status": status}


def parse_turn_notification(params: Any) -> Dict[str, Any]:
    """Parse turn/started or turn/completed notification params."""
    if not isinstance(params, dict):
        return {}
    thread_id = _safe_id(params.get("threadId") or params.get("thread_id"))
    turn = params.get("turn")
    turn_id = None
    status = None
    error_message = None
    if isinstance(turn, dict):
        turn_id = _safe_id(turn.get("id"))
        status = turn.get("status") if isinstance(turn.get("status"), str) else None
        err = turn.get("error")
        if isinstance(err, dict):
            error_message = sanitize_error_message(err.get("message") or "turn failed")
        elif isinstance(err, str):
            error_message = sanitize_error_message(err)
    return {
        "threadId": thread_id,
        "turnId": turn_id,
        "status": status,
        "errorMessage": error_message,
    }


def parse_agent_message_delta(params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    delta = params.get("delta")
    if not isinstance(delta, str):
        return {}
    return {
        "threadId": _safe_id(params.get("threadId") or params.get("thread_id")),
        "turnId": _safe_id(params.get("turnId") or params.get("turn_id")),
        "itemId": _safe_id(params.get("itemId") or params.get("item_id")),
        "delta": delta,
    }


def parse_error_notification(params: Any) -> Dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    err = params.get("error")
    message = "Codex turn error"
    if isinstance(err, dict):
        message = sanitize_error_message(err.get("message") or message)
    elif isinstance(err, str):
        message = sanitize_error_message(err)
    return {
        "threadId": _safe_id(params.get("threadId") or params.get("thread_id")),
        "turnId": _safe_id(params.get("turnId") or params.get("turn_id")),
        "message": message,
        "willRetry": bool(params.get("willRetry")),
    }


def build_thread_start_params(
    *,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    sandbox: str = "read-only",
    approval_policy: str = "on-request",
) -> dict:
    """Build allowlisted thread/start params (stable surface only)."""
    params: Dict[str, Any] = {
        "sandbox": sandbox,
        "approvalPolicy": approval_policy,
    }
    if isinstance(model, str) and model.strip():
        params["model"] = model.strip()
    if isinstance(cwd, str) and cwd.strip():
        params["cwd"] = cwd.strip()
    return params


def build_turn_start_params(*, thread_id: str, text: str) -> dict:
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("threadId required")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text required")
    return {
        "threadId": thread_id.strip(),
        "input": [{"type": "text", "text": text}],
    }


def build_turn_interrupt_params(*, thread_id: str, turn_id: str) -> dict:
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("threadId required")
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise ValueError("turnId required")
    return {"threadId": thread_id.strip(), "turnId": turn_id.strip()}


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

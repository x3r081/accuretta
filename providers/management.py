"""Provider management helpers used by bridge HTTP handlers.

Newly written for Accuretta. Keeps credential material out of responses.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

from auth.models import StoredCredential
from auth.store import AuthStore, AuthStoreInfo, create_auth_store

from .base import AuthType, ProviderDefinition
from .errors import ProviderError, ProviderNotConfigured, ProviderUnavailable
from .example_cloud import ensure_example_cloud_registered
from .local_llama import ensure_local_llama_registered
from .registry import ProviderRegistry, get_default_registry
from .selection import (
    DEFAULT_PROVIDER_ID,
    assert_provider_usable_for_chat,
    persist_provider_id,
    resolve_provider_selection,
)
from .status import (
    assert_safe_provider_payload,
    build_safe_provider_status,
    sanitize_provider_error_message,
)

_auth_info: Optional[AuthStoreInfo] = None


def ensure_builtin_providers(registry: Optional[ProviderRegistry] = None) -> ProviderRegistry:
    reg = registry or get_default_registry()
    ensure_local_llama_registered(reg)
    ensure_example_cloud_registered(reg)
    return reg


def get_auth_store_info(*, force_file: bool = False) -> AuthStoreInfo:
    """Lazy AuthStore — never raises on keyring failure."""
    global _auth_info
    if _auth_info is None or force_file:
        try:
            _auth_info = create_auth_store(force_file=force_file)
        except Exception as exc:
            # Last-resort empty file store via create_auth_store(force_file=True)
            _auth_info = create_auth_store(force_file=True)
            _auth_info = AuthStoreInfo(
                store=_auth_info.store,
                backend="file",
                secure_cloud_auth_available=False,
                detail=f"Credential store limited ({type(exc).__name__})",
            )
    return _auth_info


def reset_auth_store_info() -> None:
    global _auth_info
    _auth_info = None


def _credential_public_bits(store: AuthStore, provider_id: str) -> Tuple[bool, Any, Optional[str]]:
    try:
        cred = store.load(provider_id)
    except Exception:
        return False, None, None
    if cred is None:
        return False, None, None
    label = None
    if isinstance(cred.metadata, dict):
        raw = cred.metadata.get("account_label")
        if raw is not None:
            label = str(raw)
    return True, cred.expires_at, label


def provider_status_for(
    definition: ProviderDefinition,
    *,
    settings: dict,
    store: Optional[AuthStore] = None,
    selected_id: Optional[str] = None,
) -> dict:
    ensure_builtin_providers()
    selection = resolve_provider_selection(settings)
    effective_selected = selected_id or selection.provider_id
    authenticated = definition.auth_type == AuthType.NONE and definition.enabled
    expires_at = None
    account_label = None
    error = None

    if definition.auth_type != AuthType.NONE and store is not None:
        authenticated, expires_at, account_label = _credential_public_bits(store, definition.id)

    available = bool(definition.enabled)
    if not available:
        error = None  # disabledReason covers this

    status = build_safe_provider_status(
        definition,
        authenticated=authenticated,
        available=available,
        selected=(definition.id == effective_selected),
        is_default=(definition.id == DEFAULT_PROVIDER_ID),
        expires_at=expires_at,
        account_label=account_label,
        disabled_reason=definition.disabled_reason,
        error=error,
    )
    if selection.warning and definition.id == selection.provider_id:
        status["error"] = sanitize_provider_error_message(selection.warning)
    assert_safe_provider_payload(status)
    return status


def list_provider_statuses(settings: dict) -> dict:
    reg = ensure_builtin_providers()
    info = get_auth_store_info()
    selection = resolve_provider_selection(settings)
    providers = [
        provider_status_for(
            d,
            settings=settings,
            store=info.store,
            selected_id=selection.provider_id,
        )
        for d in reg.list_definitions(include_disabled=True)
    ]
    payload = {
        "providers": providers,
        "selectedProviderId": selection.provider_id,
        "defaultProviderId": DEFAULT_PROVIDER_ID,
        "warning": selection.warning,
        "authBackend": info.backend,
        "secureCloudAuthAvailable": info.secure_cloud_auth_available,
        "authDetail": info.detail if not info.secure_cloud_auth_available else None,
    }
    assert_safe_provider_payload(payload)
    return payload


def get_provider_status(provider_id: str, settings: dict) -> dict:
    reg = ensure_builtin_providers()
    try:
        definition = reg.get_definition(provider_id)
    except ProviderNotConfigured as exc:
        raise ProviderNotConfigured(
            f"Unknown provider: {provider_id}",
            provider_id=provider_id,
        ) from exc
    info = get_auth_store_info()
    return provider_status_for(definition, settings=settings, store=info.store)


def select_provider(provider_id: str, settings: dict, *, save: Callable[[dict], None]) -> dict:
    reg = ensure_builtin_providers()
    try:
        definition = reg.get_definition(provider_id)
    except ProviderNotConfigured as exc:
        raise ProviderNotConfigured(
            f"Unknown provider: {provider_id}",
            provider_id=provider_id,
        ) from exc
    if not definition.enabled:
        raise ProviderUnavailable(
            definition.disabled_reason or "Provider is not available",
            provider_id=provider_id,
        )
    if definition.id != DEFAULT_PROVIDER_ID:
        raise ProviderUnavailable(
            "Only Local llama.cpp can be selected in this release",
            provider_id=provider_id,
        )
    updated = persist_provider_id(settings, definition.id)
    save(updated)
    return {
        "ok": True,
        "providerId": definition.id,
        "status": get_provider_status(definition.id, updated),
    }


def disconnect_provider(provider_id: str, settings: dict) -> dict:
    reg = ensure_builtin_providers()
    try:
        definition = reg.get_definition(provider_id)
    except ProviderNotConfigured as exc:
        raise ProviderNotConfigured(
            f"Unknown provider: {provider_id}",
            provider_id=provider_id,
        ) from exc

    if definition.auth_type == AuthType.NONE:
        return {
            "ok": True,
            "providerId": provider_id,
            "disconnected": False,
            "applicable": False,
            "message": "Disconnect is not applicable for Local llama.cpp",
            "status": get_provider_status(provider_id, settings),
        }

    info = get_auth_store_info()
    deleted = False
    try:
        deleted = bool(info.store.delete(provider_id))
    except Exception:
        deleted = False
    return {
        "ok": True,
        "providerId": provider_id,
        "disconnected": deleted,
        "applicable": True,
        "message": "Credentials removed" if deleted else "No stored credentials",
        "status": get_provider_status(provider_id, settings),
    }


def list_models_for_provider(provider_id: str) -> dict:
    reg = ensure_builtin_providers()
    try:
        definition = reg.get_definition(provider_id)
    except ProviderNotConfigured as exc:
        raise ProviderNotConfigured(
            f"Unknown provider: {provider_id}",
            provider_id=provider_id,
        ) from exc
    if not definition.enabled or provider_id != DEFAULT_PROVIDER_ID:
        raise ProviderUnavailable(
            definition.disabled_reason
            or "Model listing is not available for this provider",
            provider_id=provider_id,
        )
    provider = reg.get_provider(provider_id)
    models = list(provider.list_models())
    # Strip any accidental secret-looking keys from model rows.
    safe_models: List[dict] = []
    for row in models:
        if not isinstance(row, dict):
            continue
        safe_models.append({
            k: v for k, v in row.items()
            if str(k).lower() not in {
                "access_token", "refresh_token", "authorization", "client_secret",
            }
        })
    payload = {"providerId": provider_id, "models": safe_models}
    assert_safe_provider_payload(payload)
    return payload


def resolve_chat_provider(settings: dict):
    """Resolve + validate provider for an incoming chat request."""
    ensure_builtin_providers()
    selection = resolve_provider_selection(settings)
    assert_provider_usable_for_chat(selection)
    return selection


def provider_http_error(exc: BaseException) -> Tuple[int, dict]:
    """Map provider errors to (status, safe JSON body)."""
    if isinstance(exc, ProviderNotConfigured):
        return 404, {
            "error": exc.code,
            "message": sanitize_provider_error_message(exc.message),
            "providerId": exc.provider_id,
        }
    if isinstance(exc, ProviderUnavailable):
        return 409, {
            "error": exc.code,
            "message": sanitize_provider_error_message(exc.message),
            "providerId": exc.provider_id,
        }
    if isinstance(exc, ProviderError):
        code = getattr(exc, "code", "provider_error")
        status = 401 if "authentication" in code else 400
        if code == "provider_unavailable":
            status = 409
        return status, {
            "error": code,
            "message": sanitize_provider_error_message(str(exc)),
            "providerId": getattr(exc, "provider_id", None),
        }
    return 500, {
        "error": "internal_error",
        "message": "Provider request failed",
    }

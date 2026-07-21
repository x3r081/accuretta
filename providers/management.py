"""Provider management helpers used by bridge HTTP handlers.

Newly written for Accuretta. Keeps credential material out of responses.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

from auth.store import AuthStore, AuthStoreInfo, create_auth_store

from .base import AuthType, ProviderDefinition
from .errors import (
    AuthenticationRequired,
    ProviderError,
    ProviderNotConfigured,
    ProviderUnavailable,
    RateLimited,
)
from .example_cloud import ensure_example_cloud_registered
from .github_provider import GITHUB_PROVIDER_ID, ensure_github_registered
from .codex_provider import CODEX_PROVIDER_ID, ensure_codex_registered
from .local_llama import ensure_local_llama_registered
from .openai_provider import (
    OPENAI_DEFAULT_MODEL,
    OPENAI_PROVIDER_ID,
    OpenAIProvider,
    connect_openai_api_key,
    ensure_openai_registered,
    openai_api_key_from_store,
)
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
    ensure_openai_registered(reg)
    ensure_github_registered(reg)
    ensure_codex_registered(reg)
    return reg


def get_auth_store_info(*, force_file: bool = False) -> AuthStoreInfo:
    """Lazy AuthStore — never raises on keyring failure."""
    global _auth_info
    if _auth_info is None or force_file:
        try:
            _auth_info = create_auth_store(force_file=force_file)
        except Exception as exc:
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
    credential_stored = None
    credential_validated = None
    last_validated_at = None
    model = None

    if definition.auth_type != AuthType.NONE and store is not None:
        authenticated, expires_at, account_label = _credential_public_bits(store, definition.id)
        try:
            cred = store.load(definition.id)
        except Exception:
            cred = None
        if cred is not None:
            credential_stored = True
            meta = cred.metadata if isinstance(cred.metadata, dict) else {}
            credential_validated = bool(meta.get("credential_validated"))
            last_validated_at = meta.get("validated_at")
            if definition.auth_type == AuthType.API_KEY:
                authenticated = True
                if not account_label:
                    account_label = str(meta.get("account_label") or "API key stored")
        else:
            credential_stored = False
            credential_validated = False
            authenticated = False

    if definition.id == OPENAI_PROVIDER_ID and isinstance(settings, dict):
        model = settings.get("openai_model") or OPENAI_DEFAULT_MODEL

    status = build_safe_provider_status(
        definition,
        authenticated=authenticated,
        available=bool(definition.enabled),
        selected=(definition.id == effective_selected),
        is_default=(definition.id == DEFAULT_PROVIDER_ID),
        expires_at=expires_at,
        account_label=account_label,
        disabled_reason=definition.disabled_reason,
        error=error,
        credential_stored=credential_stored,
        credential_validated=credential_validated,
        model=model,
        last_validated_at=last_validated_at,
    )
    if selection.warning and definition.id == selection.provider_id:
        status["error"] = sanitize_provider_error_message(selection.warning)
    assert_safe_provider_payload(status)
    return status


def list_provider_statuses(settings: dict) -> dict:
    reg = ensure_builtin_providers()
    info = get_auth_store_info()
    selection = resolve_provider_selection(settings)
    providers = []
    for d in reg.list_definitions(include_disabled=True):
        if d.id == CODEX_PROVIDER_ID:
            from providers.codex_provider import get_codex_provider_status
            providers.append(get_codex_provider_status(live=False))
        else:
            providers.append(
                provider_status_for(
                    d,
                    settings=settings,
                    store=info.store,
                    selected_id=selection.provider_id,
                )
            )
    payload = {
        "providers": providers,
        "selectedProviderId": selection.provider_id,
        "defaultProviderId": DEFAULT_PROVIDER_ID,
        "warning": selection.warning,
        "authBackend": info.backend,
        "secureCloudAuthAvailable": info.secure_cloud_auth_available,
        "authDetail": info.detail if not info.secure_cloud_auth_available else None,
        "diagnostics": build_provider_diagnostics(settings, providers=providers, info=info),
    }
    assert_safe_provider_payload(payload)
    return payload


def build_provider_diagnostics(
    settings: dict,
    *,
    providers: Optional[List[dict]] = None,
    info: Optional[AuthStoreInfo] = None,
) -> dict:
    """Safe milestone diagnostics for UI / smoke tests — never secrets."""
    ensure_builtin_providers()
    if info is None:
        info = get_auth_store_info()
    if providers is None:
        # Avoid recursion through list_provider_statuses.
        selection = resolve_provider_selection(settings)
        providers = [
            provider_status_for(
                d,
                settings=settings,
                store=info.store,
                selected_id=selection.provider_id,
            )
            for d in get_default_registry().list_definitions(include_disabled=True)
        ]
    by_id = {p.get("providerId"): p for p in providers if isinstance(p, dict)}
    local = by_id.get(DEFAULT_PROVIDER_ID) or {}
    openai = by_id.get(OPENAI_PROVIDER_ID) or {}
    github = by_id.get(GITHUB_PROVIDER_ID) or {}
    codex = by_id.get(CODEX_PROVIDER_ID) or {}
    out = {
        "localProviderAvailable": bool(local.get("available", True)),
        "openaiAvailable": bool(openai.get("available")),
        "openaiCredentialStored": bool(openai.get("credentialStored")),
        "openaiCredentialValidated": openai.get("credentialValidated"),
        "githubConfigured": bool(github.get("available")),
        "githubAvailable": bool(github.get("available")),
        "githubDisabledReason": github.get("disabledReason"),
        "codexInstalled": bool(codex.get("installed") if "installed" in codex else codex.get("available")),
        "codexAvailable": bool(codex.get("available")),
        "codexDisabledReason": codex.get("disabledReason"),
        "authBackend": info.backend,
        "secureCloudAuthAvailable": bool(info.secure_cloud_auth_available),
        "selectedProviderId": resolve_provider_selection(settings).provider_id,
    }
    assert_safe_provider_payload(out)
    return out


def shutdown_provider_background() -> None:
    """Cancel device-flow pollers and stop Codex app-server.

    Safe to call multiple times. Must run before ``os._exit`` (which skips atexit).
    """
    try:
        from auth.device_flow import reset_device_flow_manager
        reset_device_flow_manager()
    except Exception:
        pass
    try:
        from codex.session import shutdown_codex
        shutdown_codex()
    except Exception:
        pass


def get_provider_status(provider_id: str, settings: dict) -> dict:
    reg = ensure_builtin_providers()
    try:
        definition = reg.get_definition(provider_id)
    except ProviderNotConfigured as exc:
        raise ProviderNotConfigured(
            f"Unknown provider: {provider_id}",
            provider_id=provider_id,
        ) from exc
    if provider_id == CODEX_PROVIDER_ID:
        from providers.codex_provider import get_codex_provider_status
        # Live status starts app-server lazily when Codex is installed.
        return get_codex_provider_status(live=True)
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
    info = get_auth_store_info()
    if definition.id == OPENAI_PROVIDER_ID:
        if not openai_api_key_from_store(info.store):
            raise AuthenticationRequired(
                "Connect an OpenAI API key before selecting OpenAI",
                provider_id=provider_id,
            )
    elif definition.id == GITHUB_PROVIDER_ID:
        raise ProviderUnavailable(
            "GitHub connects your account only and cannot be selected for chat. "
            "Copilot inference is not enabled.",
            provider_id=provider_id,
        )
    elif definition.id == CODEX_PROVIDER_ID:
        raise ProviderUnavailable(
            "ChatGPT / Codex authentication is available, but Codex inference "
            "is not enabled in this milestone.",
            provider_id=provider_id,
        )
    elif definition.id != DEFAULT_PROVIDER_ID:
        raise ProviderUnavailable(
            "This provider cannot be selected yet",
            provider_id=provider_id,
        )
    updated = persist_provider_id(settings, definition.id)
    if definition.id == OPENAI_PROVIDER_ID and not (updated.get("openai_model") or "").strip():
        updated["openai_model"] = OPENAI_DEFAULT_MODEL
    save(updated)
    return {
        "ok": True,
        "providerId": definition.id,
        "status": get_provider_status(definition.id, updated),
    }


def connect_provider(provider_id: str, body: dict, settings: dict) -> dict:
    ensure_builtin_providers()
    if provider_id == CODEX_PROVIDER_ID:
        from providers.codex_provider import codex_connect_browser
        method = (body or {}).get("method") if isinstance(body, dict) else None
        if method and str(method).lower() not in {"browser", "chatgpt"}:
            raise ProviderUnavailable(
                "Unsupported Codex connect method",
                provider_id=provider_id,
            )
        return codex_connect_browser()
    if provider_id != OPENAI_PROVIDER_ID:
        raise ProviderUnavailable(
            "Connect is not available for this provider",
            provider_id=provider_id,
        )
    api_key = body.get("apiKey") if isinstance(body, dict) else None
    info = get_auth_store_info()
    try:
        result = connect_openai_api_key(info.store, api_key or "")
    except RateLimited:
        status = get_provider_status(OPENAI_PROVIDER_ID, settings)
        payload = {
            "ok": True,
            "providerId": OPENAI_PROVIDER_ID,
            "credentialStored": True,
            "credentialValidated": False,
            "status": status,
            "message": "API key stored; validation rate-limited — try listing models later",
        }
        assert_safe_provider_payload(payload)
        return payload
    status = get_provider_status(OPENAI_PROVIDER_ID, settings)
    payload = {
        "ok": True,
        "providerId": OPENAI_PROVIDER_ID,
        "credentialStored": result.get("credentialStored", True),
        "credentialValidated": result.get("credentialValidated", False),
        "status": status,
        "message": (
            "API key saved and validated"
            if result.get("credentialValidated")
            else "API key saved"
        ),
    }
    assert_safe_provider_payload(payload)
    return payload


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

    if provider_id == CODEX_PROVIDER_ID:
        from providers.codex_provider import codex_logout
        return codex_logout()

    info = get_auth_store_info()
    deleted = False
    try:
        deleted = bool(info.store.delete(provider_id))
    except Exception:
        deleted = False
    # Cancel any in-flight device authorization for this provider.
    try:
        from auth.device_flow import get_device_flow_manager
        get_device_flow_manager().cancel(provider_id)
    except Exception:
        pass
    return {
        "ok": True,
        "providerId": provider_id,
        "disconnected": deleted,
        "applicable": True,
        "message": "Credentials removed" if deleted else "No stored credentials",
        "status": get_provider_status(provider_id, settings),
    }


def list_models_for_provider(provider_id: str, settings: Optional[dict] = None) -> dict:
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
            definition.disabled_reason
            or "Model listing is not available for this provider",
            provider_id=provider_id,
        )

    if provider_id == DEFAULT_PROVIDER_ID:
        provider = reg.get_provider(provider_id)
        models = list(provider.list_models())
    elif provider_id == OPENAI_PROVIDER_ID:
        info = get_auth_store_info()
        api_key = openai_api_key_from_store(info.store)
        if not api_key:
            raise AuthenticationRequired(
                "OpenAI API key required to list models",
                provider_id=provider_id,
            )
        provider = OpenAIProvider()
        models = list(provider.list_models(api_key=api_key))
    else:
        raise ProviderUnavailable(
            "Model listing is not available for this provider",
            provider_id=provider_id,
        )

    safe_models: List[dict] = []
    for row in models:
        if not isinstance(row, dict):
            continue
        safe_models.append({
            k: v for k, v in row.items()
            if str(k).lower() not in {
                "access_token", "refresh_token", "authorization", "client_secret", "api_key",
            }
        })
    selected_model = None
    if isinstance(settings, dict) and provider_id == OPENAI_PROVIDER_ID:
        selected_model = settings.get("openai_model") or OPENAI_DEFAULT_MODEL
    payload = {
        "providerId": provider_id,
        "models": safe_models,
        "selectedModel": selected_model,
    }
    assert_safe_provider_payload(payload)
    return payload


def start_device_authorization(provider_id: str, settings: dict) -> dict:
    """Start RFC 8628 device flow for a registered provider. Safe response only."""
    ensure_builtin_providers()
    from auth.device_flow import (
        DeviceFlowError,
        get_device_flow_manager,
        resolve_device_config,
    )
    from auth.oauth_client import TokenExchangeFailed

    reg = get_default_registry()
    try:
        definition = reg.get_definition(provider_id)
    except ProviderNotConfigured as exc:
        raise ProviderNotConfigured(
            f"Unknown provider: {provider_id}",
            provider_id=provider_id,
        ) from exc
    if definition.auth_type != AuthType.OAUTH_DEVICE:
        raise ProviderUnavailable(
            "Device authorization is not available for this provider",
            provider_id=provider_id,
        )
    if not definition.enabled:
        raise ProviderUnavailable(
            definition.disabled_reason or "Provider is not available",
            provider_id=provider_id,
        )
    config = resolve_device_config(provider_id)
    if config is None:
        raise ProviderUnavailable(
            definition.disabled_reason or "Device authorization is not configured",
            provider_id=provider_id,
        )
    info = get_auth_store_info()
    manager = get_device_flow_manager()
    on_success = None
    store = info.store
    if provider_id == GITHUB_PROVIDER_ID:
        from providers.github_provider import store_github_device_result

        def on_success(result):
            store_github_device_result(info.store, result)

        store = None  # github handler persists after validation
    try:
        payload = manager.start(config, store=store, on_success=on_success)
    except DeviceFlowError as exc:
        raise ProviderUnavailable(str(exc), provider_id=provider_id) from None
    except TokenExchangeFailed as exc:
        raise ProviderUnavailable(
            sanitize_provider_error_message(str(exc)),
            provider_id=provider_id,
        ) from None
    assert_safe_provider_payload(payload)
    if "deviceCode" in payload or "device_code" in payload:
        raise ValueError("device_code leaked into start payload")
    return payload


def device_authorization_status(provider_id: str, settings: dict) -> dict:
    ensure_builtin_providers()
    from auth.device_flow import get_device_flow_manager, resolve_device_config

    reg = get_default_registry()
    try:
        reg.get_definition(provider_id)
    except ProviderNotConfigured as exc:
        raise ProviderNotConfigured(
            f"Unknown provider: {provider_id}",
            provider_id=provider_id,
        ) from exc
    _ = resolve_device_config(provider_id)
    _ = settings
    payload = get_device_flow_manager().status(provider_id)
    if payload.get("status") == "authorized":
        payload = dict(payload)
        payload["providerStatus"] = get_provider_status(provider_id, settings)
    assert_safe_provider_payload(payload)
    return payload


def cancel_device_authorization(provider_id: str, settings: dict) -> dict:
    ensure_builtin_providers()
    from auth.device_flow import get_device_flow_manager

    reg = get_default_registry()
    try:
        reg.get_definition(provider_id)
    except ProviderNotConfigured as exc:
        raise ProviderNotConfigured(
            f"Unknown provider: {provider_id}",
            provider_id=provider_id,
        ) from exc
    _ = settings
    payload = get_device_flow_manager().cancel(provider_id)
    assert_safe_provider_payload(payload)
    return payload


def resolve_chat_provider(settings: dict):
    ensure_builtin_providers()
    selection = resolve_provider_selection(settings)
    info = get_auth_store_info()
    assert_provider_usable_for_chat(selection, auth_store=info.store)
    return selection


def provider_http_error(exc: BaseException) -> Tuple[int, dict]:
    if isinstance(exc, ProviderNotConfigured):
        return 404, {
            "error": exc.code,
            "message": sanitize_provider_error_message(exc.message),
            "providerId": exc.provider_id,
        }
    if isinstance(exc, AuthenticationRequired):
        return 401, {
            "error": exc.code,
            "message": sanitize_provider_error_message(exc.message),
            "providerId": exc.provider_id,
        }
    if isinstance(exc, RateLimited):
        return 429, {
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
        if code == "rate_limited":
            status = 429
        return status, {
            "error": code,
            "message": sanitize_provider_error_message(str(exc)),
            "providerId": getattr(exc, "provider_id", None),
        }
    return 500, {
        "error": "internal_error",
        "message": "Provider request failed",
    }

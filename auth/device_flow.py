"""OAuth 2.0 device-authorization flow manager (RFC 8628).

Newly written for Accuretta. Provider-configured and reusable — does not
hard-code GitHub (or any vendor) behavior.

Security:
- device_code never returned to app.js / never logged
- tokens never appear in status DTOs
- only one active flow per provider_id
- stale pollers cannot overwrite newer credentials
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from auth.models import StoredCredential
from auth.oauth_client import TokenResponse, parse_token_response, request_device_authorization
from auth.redact import redact_sensitive_text
from auth.store import AuthStore

from .device_models import (
    DeviceAuthorizationConfig,
    DeviceAuthorizationResult,
    DeviceAuthorizationSession,
    DeviceFlowStatus,
    safe_device_start_payload,
    safe_device_status_payload,
)

log = logging.getLogger("accuretta.auth.device_flow")

PostForm = Callable[[str, Dict[str, str]], Dict[str, Any]]
SleepFn = Callable[[float], bool]  # returns False if cancelled

_DEVICE_CONFIG_FACTORIES: Dict[str, Callable[[], DeviceAuthorizationConfig]] = {}
_MANAGER: Optional["DeviceFlowManager"] = None
_MANAGER_LOCK = threading.Lock()


def register_device_config_factory(
    provider_id: str,
    factory: Callable[[], DeviceAuthorizationConfig],
) -> None:
    _DEVICE_CONFIG_FACTORIES[provider_id] = factory


def unregister_device_config_factory(provider_id: str) -> None:
    _DEVICE_CONFIG_FACTORIES.pop(provider_id, None)


def clear_device_config_factories() -> None:
    _DEVICE_CONFIG_FACTORIES.clear()


def resolve_device_config(provider_id: str) -> Optional[DeviceAuthorizationConfig]:
    factory = _DEVICE_CONFIG_FACTORIES.get(provider_id)
    if factory is None:
        return None
    return factory()


def get_device_flow_manager() -> "DeviceFlowManager":
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = DeviceFlowManager()
        return _MANAGER


def reset_device_flow_manager() -> None:
    """Test helper — cancel and drop the singleton."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            _MANAGER.shutdown()
        _MANAGER = None


class DeviceFlowError(Exception):
    def __init__(self, message: str, *, status: DeviceFlowStatus = DeviceFlowStatus.FAILED):
        super().__init__(message)
        self.status = status


class DeviceFlowManager:
    """Tracks in-flight device sessions and background token polling."""

    def __init__(
        self,
        *,
        post_form: Optional[PostForm] = None,
        request_device: Optional[Callable[..., DeviceAuthorizationSession]] = None,
        sleep: Optional[SleepFn] = None,
        monotonic: Optional[Callable[[], float]] = None,
    ):
        self._lock = threading.RLock()
        self._sessions: Dict[str, DeviceAuthorizationSession] = {}
        self._cancel_events: Dict[str, threading.Event] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._generations: Dict[str, int] = {}
        self._terminal: Dict[str, DeviceFlowStatus] = {}
        self._terminal_error: Dict[str, str] = {}
        self._shutdown = threading.Event()
        self._post_form = post_form
        self._request_device = request_device or request_device_authorization
        self._sleep = sleep
        self._monotonic = monotonic or time.monotonic
        self._wall = time.time

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._lock:
            providers = list(self._cancel_events.keys())
        for pid in providers:
            self.cancel(pid)
        with self._lock:
            self._sessions.clear()
            self._terminal.clear()
            self._terminal_error.clear()

    def start(
        self,
        config: DeviceAuthorizationConfig,
        *,
        store: Optional[AuthStore] = None,
        on_success: Optional[Callable[[DeviceAuthorizationResult], None]] = None,
    ) -> dict:
        """Start (or supersede) a device flow. Returns frontend-safe payload."""
        if self._shutdown.is_set():
            raise DeviceFlowError("Device authorization is shutting down")

        # Supersede any prior flow for this provider.
        self.cancel(config.provider_id, reason="superseded")

        session = self._request_device(config, post_form=self._post_form)
        # request_device_authorization returns a session-like object — normalize.
        if not isinstance(session, DeviceAuthorizationSession):
            raise DeviceFlowError("Invalid device authorization response")

        with self._lock:
            gen = self._generations.get(config.provider_id, 0) + 1
            self._generations[config.provider_id] = gen
            session.generation = gen
            session.status = DeviceFlowStatus.PENDING
            self._sessions[config.provider_id] = session
            self._terminal.pop(config.provider_id, None)
            self._terminal_error.pop(config.provider_id, None)
            cancel_ev = threading.Event()
            self._cancel_events[config.provider_id] = cancel_ev

        thread = threading.Thread(
            target=self._poll_loop,
            name=f"device-poll-{config.provider_id}-{gen}",
            args=(config, session, cancel_ev, store, on_success),
            daemon=True,
        )
        with self._lock:
            self._threads[config.provider_id] = thread
        thread.start()
        payload = safe_device_start_payload(session)
        # Never log device_code / tokens.
        log.info(
            "device flow started provider=%s user_code=%s expires_at=%s",
            config.provider_id,
            session.user_code,
            session.expires_at,
        )
        return payload

    def status(self, provider_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(provider_id)
            if session is not None and session.status == DeviceFlowStatus.PENDING:
                return safe_device_start_payload(session)
            if provider_id in self._terminal:
                return safe_device_status_payload(
                    provider_id,
                    status=self._terminal[provider_id],
                    error=self._terminal_error.get(provider_id),
                )
            if session is not None:
                return safe_device_status_payload(
                    provider_id,
                    status=session.status,
                    session=session,
                )
        return safe_device_status_payload(provider_id, status=DeviceFlowStatus.IDLE)

    def cancel(self, provider_id: str, *, reason: str = "cancelled") -> dict:
        with self._lock:
            cancel_ev = self._cancel_events.get(provider_id)
            session = self._sessions.get(provider_id)
            if cancel_ev is not None:
                cancel_ev.set()
            if session is not None and session.status == DeviceFlowStatus.PENDING:
                session.status = DeviceFlowStatus.CANCELLED
                session.error = None
            self._terminal[provider_id] = DeviceFlowStatus.CANCELLED
            self._terminal_error.pop(provider_id, None)
            # Drop sensitive session material promptly.
            self._sessions.pop(provider_id, None)
            self._cancel_events.pop(provider_id, None)
        return safe_device_status_payload(provider_id, status=DeviceFlowStatus.CANCELLED)

    def _sleep_interval(self, seconds: float, cancel_ev: threading.Event) -> bool:
        """Sleep; return False if cancelled or shutting down."""
        if self._sleep is not None:
            return self._sleep(seconds)
        deadline = self._monotonic() + max(0.0, float(seconds))
        while self._monotonic() < deadline:
            if cancel_ev.is_set() or self._shutdown.is_set():
                return False
            time.sleep(min(0.05, deadline - self._monotonic()))
        return not (cancel_ev.is_set() or self._shutdown.is_set())

    def _poll_loop(
        self,
        config: DeviceAuthorizationConfig,
        session: DeviceAuthorizationSession,
        cancel_ev: threading.Event,
        store: Optional[AuthStore],
        on_success: Optional[Callable[[DeviceAuthorizationResult], None]],
    ) -> None:
        generation = session.generation
        interval = max(
            float(config.min_poll_interval_seconds),
            float(session.interval),
        )
        interval_cap = float(config.max_poll_interval_seconds)
        post = self._post_form
        if post is None:
            from auth.oauth_client import _post_form as default_post

            def post(url: str, data: Dict[str, str]) -> Dict[str, Any]:
                return default_post(url, data, return_error_payload=True)

        # Initial wait before first poll (RFC 8628).
        if not self._sleep_interval(interval, cancel_ev):
            self._mark_terminal(config.provider_id, generation, DeviceFlowStatus.CANCELLED)
            return

        while True:
            if cancel_ev.is_set() or self._shutdown.is_set():
                self._mark_terminal(config.provider_id, generation, DeviceFlowStatus.CANCELLED)
                return
            if self._wall() >= session.expires_at:
                self._mark_terminal(config.provider_id, generation, DeviceFlowStatus.EXPIRED)
                return

            try:
                payload = self._token_poll_once(config, session, post)
            except _PermanentDeviceError as exc:
                status = exc.status
                self._mark_terminal(
                    config.provider_id,
                    generation,
                    status,
                    error=str(exc),
                )
                return
            except _TransientDeviceError:
                if not self._sleep_interval(interval, cancel_ev):
                    self._mark_terminal(config.provider_id, generation, DeviceFlowStatus.CANCELLED)
                    return
                continue
            except Exception as exc:
                msg = redact_sensitive_text(f"Device authorization failed ({type(exc).__name__})")
                self._mark_terminal(
                    config.provider_id,
                    generation,
                    DeviceFlowStatus.FAILED,
                    error=msg,
                )
                return

            if payload is None:
                # authorization_pending
                if not self._sleep_interval(interval, cancel_ev):
                    self._mark_terminal(config.provider_id, generation, DeviceFlowStatus.CANCELLED)
                    return
                continue

            if isinstance(payload, dict) and payload.get("__slow_down__"):
                interval = min(interval + 5.0, interval_cap)
                if not self._sleep_interval(interval, cancel_ev):
                    self._mark_terminal(config.provider_id, generation, DeviceFlowStatus.CANCELLED)
                    return
                continue

            # Success — TokenResponse
            assert isinstance(payload, TokenResponse)
            result = _token_to_result(config, payload)
            if not self._is_current(config.provider_id, generation):
                # Stale poller — do not save credentials.
                return
            try:
                if on_success is not None:
                    on_success(result)
                elif store is not None:
                    _save_result(store, config.provider_id, result)
            except Exception as exc:
                msg = redact_sensitive_text(
                    f"Failed to store credentials ({type(exc).__name__})"
                )
                self._mark_terminal(
                    config.provider_id,
                    generation,
                    DeviceFlowStatus.FAILED,
                    error=msg,
                )
                return
            self._mark_terminal(config.provider_id, generation, DeviceFlowStatus.AUTHORIZED)
            return

    def _token_poll_once(
        self,
        config: DeviceAuthorizationConfig,
        session: DeviceAuthorizationSession,
        post: PostForm,
    ):
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": config.client_id,
            "device_code": session.device_code,
        }
        for key, value in (config.extra_token_parameters or {}).items():
            data[str(key)] = str(value)
        if config.client_secret:
            data["client_secret"] = config.client_secret

        try:
            payload = post(config.token_endpoint, data)
        except Exception as exc:
            # Map HTTP-ish failures without leaking bodies.
            status = getattr(getattr(exc, "__cause__", None), "status_code", None)
            oauth_error = getattr(getattr(exc, "__cause__", None), "oauth_error", None)
            if status in (401, 403) or oauth_error in {"invalid_client", "unauthorized_client"}:
                raise _PermanentDeviceError(
                    "OAuth client configuration was rejected",
                    status=DeviceFlowStatus.FAILED,
                ) from None
            if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
                raise _TransientDeviceError("transient") from None
            # urllib / network
            name = type(exc).__name__
            if name in {"URLError", "TimeoutError", "OSError", "ConnectionError"}:
                raise _TransientDeviceError("transient") from None
            # TokenExchangeFailed without HTTP carrier — treat as transient unless known permanent codes.
            raise _TransientDeviceError("transient") from None

        if not isinstance(payload, dict):
            raise _PermanentDeviceError(
                "Malformed token response",
                status=DeviceFlowStatus.FAILED,
            )

        http_status = payload.get("_http_status")
        if http_status == 429 or (isinstance(http_status, int) and 500 <= http_status <= 599):
            raise _TransientDeviceError("transient")

        if payload.get("access_token"):
            try:
                return parse_token_response(payload)
            except Exception:
                raise _PermanentDeviceError(
                    "Malformed token response",
                    status=DeviceFlowStatus.FAILED,
                ) from None

        error = str(payload.get("error") or "").strip().lower()
        if error == "server_error" and (
            http_status is None or (isinstance(http_status, int) and http_status >= 500)
        ):
            raise _TransientDeviceError("transient")
        if error == "authorization_pending":
            return None
        if error == "slow_down":
            return {"__slow_down__": True}
        if error in {"access_denied", "authorization_declined"}:
            raise _PermanentDeviceError(
                "Authorization was denied",
                status=DeviceFlowStatus.DENIED,
            )
        if error in {"expired_token", "expired_token_code"}:
            raise _PermanentDeviceError(
                "Device code expired",
                status=DeviceFlowStatus.EXPIRED,
            )
        if error in {"invalid_client", "unauthorized_client"}:
            raise _PermanentDeviceError(
                "OAuth client configuration was rejected",
                status=DeviceFlowStatus.FAILED,
            )
        if error in {"invalid_grant", "invalid_request"}:
            raise _PermanentDeviceError(
                "Device authorization failed",
                status=DeviceFlowStatus.FAILED,
            )
        # Unknown error — fail closed without deleting unrelated credentials.
        raise _PermanentDeviceError(
            redact_sensitive_text(str(payload.get("error_description") or error or "failed")),
            status=DeviceFlowStatus.FAILED,
        )

    def _is_current(self, provider_id: str, generation: int) -> bool:
        with self._lock:
            return self._generations.get(provider_id) == generation

    def _mark_terminal(
        self,
        provider_id: str,
        generation: int,
        status: DeviceFlowStatus,
        *,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            if self._generations.get(provider_id) != generation:
                return
            self._terminal[provider_id] = status
            if error:
                self._terminal_error[provider_id] = redact_sensitive_text(error)[:240]
            elif provider_id in self._terminal_error and status != DeviceFlowStatus.FAILED:
                self._terminal_error.pop(provider_id, None)
            session = self._sessions.pop(provider_id, None)
            if session is not None:
                session.status = status
                if error:
                    session.error = self._terminal_error.get(provider_id)
            self._cancel_events.pop(provider_id, None)
            self._threads.pop(provider_id, None)


class _PermanentDeviceError(Exception):
    def __init__(self, message: str, *, status: DeviceFlowStatus):
        super().__init__(message)
        self.status = status


class _TransientDeviceError(Exception):
    pass


def _token_to_result(
    config: DeviceAuthorizationConfig,
    token: TokenResponse,
) -> DeviceAuthorizationResult:
    expires_at = None
    if token.expires_in is not None:
        expires_at = time.time() + float(token.expires_in)
    scopes: tuple[str, ...] = ()
    if token.scope:
        scopes = tuple(s for s in str(token.scope).split() if s)
    elif config.scopes:
        scopes = tuple(config.scopes)
    return DeviceAuthorizationResult(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        expires_at=expires_at,
        token_type=token.token_type or "Bearer",
        scopes=scopes,
        provider_metadata={},
    )


def _save_result(store: AuthStore, provider_id: str, result: DeviceAuthorizationResult) -> None:
    cred = StoredCredential(
        provider_id=provider_id,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_at=result.expires_at,
        token_type=result.token_type or "Bearer",
        scopes=list(result.scopes),
        metadata={
            "credential_type": "oauth_device",
            "credential_stored": True,
            **(result.provider_metadata or {}),
        },
    )
    store.save(provider_id, cred)

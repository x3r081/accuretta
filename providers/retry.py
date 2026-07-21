"""Bounded retry helpers for provider HTTP calls.

Newly written for Accuretta. Retries only safe pre-stream / listing failures.
Never logs Authorization headers or API keys.
"""

from __future__ import annotations

import random
import time
from typing import Callable, Optional, TypeVar

from .errors import AuthenticationRequired, ProviderError, RateLimited

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_S = 0.4
DEFAULT_MAX_DELAY_S = 8.0
DEFAULT_MAX_RETRY_AFTER_S = 30.0


def parse_retry_after(value: Optional[str], *, cap: float = DEFAULT_MAX_RETRY_AFTER_S) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, cap)


def compute_backoff_seconds(
    attempt: int,
    *,
    base: float = DEFAULT_BASE_DELAY_S,
    cap: float = DEFAULT_MAX_DELAY_S,
    retry_after: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> float:
    if retry_after is not None:
        return max(0.0, min(float(retry_after), cap))
    # attempt is 0-based after first failure
    exp = min(cap, base * (2 ** max(0, attempt)))
    jitter = (rng or random).uniform(0, exp * 0.25)
    return min(cap, exp + jitter)


def is_retryable_provider_error(exc: BaseException) -> bool:
    if isinstance(exc, AuthenticationRequired):
        return False
    if isinstance(exc, RateLimited):
        return True
    if isinstance(exc, ProviderError):
        code = getattr(exc, "code", "") or ""
        if code in {"provider_unavailable", "rate_limited"}:
            return True
        if code in {
            "authentication_required",
            "authentication_expired",
            "invalid_provider_response",
            "provider_not_configured",
        }:
            return False
        # HTTP 400-class wrapped as ProviderUnavailable with status in message — not retryable
        # unless explicitly rate/unavailable above.
        return code == "provider_unavailable"
    # Transport-ish
    name = type(exc).__name__
    return name in {"TimeoutError", "URLError", "ConnectionError", "OSError"}


def cancellable_sleep(seconds: float, cancel_ev=None, *, slice_s: float = 0.05) -> bool:
    """Sleep up to ``seconds``. Returns False if cancelled."""
    if seconds <= 0:
        return True
    deadline = time.monotonic() + float(seconds)
    while True:
        if cancel_ev is not None and cancel_ev.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(slice_s, remaining))


def with_retries(
    fn: Callable[[], T],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    cancel_ev=None,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
    rng: Optional[random.Random] = None,
    sleeper: Optional[Callable[[float], bool]] = None,
) -> T:
    """Run ``fn`` with bounded retries for retryable failures.

    Does not retry after the callable has successfully returned — callers must
    not use this around partial stream readers.
    """
    attempts = max(1, int(max_attempts))
    last_exc: Optional[BaseException] = None
    sleep_fn = sleeper or (lambda s: cancellable_sleep(s, cancel_ev))
    for attempt in range(attempts):
        if cancel_ev is not None and cancel_ev.is_set():
            raise ProviderError("cancelled", provider_id=None)
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts - 1 or not is_retryable_provider_error(exc):
                raise
            retry_after = getattr(exc, "retry_after", None)
            delay = compute_backoff_seconds(
                attempt,
                base=base_delay,
                cap=max_delay,
                retry_after=retry_after,
                rng=rng,
            )
            if not sleep_fn(delay):
                raise ProviderError("cancelled", provider_id=None) from None
    assert last_exc is not None
    raise last_exc

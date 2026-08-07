"""Per-provider circuit breaker for the LLM layer.

The breaker has three states:

* ``closed`` — normal operation; every request is allowed through. Failures
  accumulate until they hit ``failure_threshold`` and trip the breaker to
  ``open``.
* ``open`` — the provider is considered down. Requests are rejected
  immediately with :class:`CircuitOpen` until ``reset_timeout_s`` elapses
  since the last failure, at which point the breaker transitions to
  ``half_open`` and admits a single probe request.
* ``half_open`` — exactly one probe is admitted. If it succeeds, the breaker
  closes; if it fails, the breaker re-opens and the reset window restarts.

State is held in-process (``_breakers``). This is appropriate for a single
worker; for a multi-worker deployment a Redis-backed implementation would be
the natural next step so failures are observed across processes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal


class CircuitOpen(Exception):
    """Raised when a request is rejected because the provider's breaker is open."""

    def __init__(self, provider: str, retry_after_s: float):
        self.provider = provider
        self.retry_after_s = retry_after_s
        super().__init__(
            f"circuit open for provider '{provider}' (retry after {retry_after_s:.3f}s)"
        )


class AllProvidersFailed(Exception):
    """Raised when every endpoint in a :class:`~orchestration.llm.fallback.FallbackChain` failed."""


CircuitState = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    """Three-state circuit breaker for a single provider.

    Parameters
    ----------
    provider:
        Identifier used for diagnostics and the module-level singleton map.
    failure_threshold:
        Number of consecutive failures that trip the breaker from ``closed``
        to ``open``. Defaults to 5 per spec §5.7.
    reset_timeout_s:
        Seconds the breaker must stay ``open`` before a probe is admitted.
        Defaults to 60 per spec.
    """

    def __init__(
        self,
        provider: str,
        failure_threshold: int = 5,
        reset_timeout_s: float = 60.0,
    ):
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        if reset_timeout_s <= 0:
            raise ValueError("reset_timeout_s must be positive")
        self.provider = provider
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self.state: CircuitState = "closed"
        self.failure_count: int = 0
        self.last_failure_at: datetime | None = None

    async def allow(self) -> bool:
        """Return ``True`` if a request may proceed right now.

        Side effect: when the breaker is ``open`` and the reset window has
        elapsed, it transitions to ``half_open`` so the next caller is the
        single probe. The probe must call :meth:`record_success` or
        :meth:`record_failure` to close or re-open the circuit.
        """
        if self.state == "closed":
            return True
        if self.state == "open":
            now = datetime.now(UTC)
            if self.last_failure_at is None:
                # Defensive: open without a recorded failure is treated as
                # immediately recoverable so callers aren't stuck forever.
                self.state = "half_open"
                return True
            elapsed = (now - self.last_failure_at).total_seconds()
            if elapsed >= self.reset_timeout_s:
                self.state = "half_open"
                return True
            return False
        # half_open: a single probe is admitted; caller must report back.
        return True

    def record_success(self) -> None:
        """Mark a successful call. Resets the breaker to ``closed``."""
        self.failure_count = 0
        self.state = "closed"
        self.last_failure_at = None

    def record_failure(self) -> None:
        """Mark a failed call. May trip the breaker to ``open``."""
        self.failure_count += 1
        if self.state == "half_open" or self.failure_count >= self.failure_threshold:
            self.state = "open"
            self.last_failure_at = datetime.now(UTC)

    def retry_after(self) -> float:
        """Seconds until the breaker may transition from ``open`` to ``half_open``."""
        if self.state != "open" or self.last_failure_at is None:
            return 0.0
        now = datetime.now(UTC)
        elapsed = (now - self.last_failure_at).total_seconds()
        remaining = self.reset_timeout_s - elapsed
        return max(0.0, remaining)

    def reset(self) -> None:
        """Force the breaker back to ``closed``. Useful for tests."""
        self.state = "closed"
        self.failure_count = 0
        self.last_failure_at = None


_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(
    provider: str,
    failure_threshold: int = 5,
    reset_timeout_s: float = 60.0,
) -> CircuitBreaker:
    """Return the module-level singleton :class:`CircuitBreaker` for ``provider``.

    Existing breakers keep their current state. Tests that need a fresh
    breaker should call :func:`reset_breakers` or :meth:`CircuitBreaker.reset`.
    """
    breaker = _breakers.get(provider)
    if breaker is None:
        breaker = CircuitBreaker(
            provider,
            failure_threshold=failure_threshold,
            reset_timeout_s=reset_timeout_s,
        )
        _breakers[provider] = breaker
    return breaker


def reset_breakers() -> None:
    """Clear the module-level breaker cache (test helper)."""
    _breakers.clear()

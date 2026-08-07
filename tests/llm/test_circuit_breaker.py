"""Tests for the per-provider circuit breaker."""

from datetime import timedelta

import pytest

from orchestration.llm.circuit_breaker import (
    AllProvidersFailed,
    CircuitBreaker,
    CircuitOpen,
    get_breaker,
    reset_breakers,
)


@pytest.fixture(autouse=True)
def _isolate_breakers():
    """Reset the module-level breaker cache so each test starts clean."""
    reset_breakers()
    yield
    reset_breakers()


# --- state machine: closed ---------------------------------------------------


async def test_closed_allows_by_default():
    cb = CircuitBreaker("p", failure_threshold=3, reset_timeout_s=1)
    assert cb.state == "closed"
    assert await cb.allow() is True


async def test_closed_trips_to_open_after_threshold_failures():
    cb = CircuitBreaker("p", failure_threshold=3, reset_timeout_s=1)
    for _ in range(3):
        assert cb.state == "closed"
        cb.record_failure()
    assert cb.state == "open"
    assert cb.failure_count == 3
    assert cb.last_failure_at is not None


async def test_record_success_resets_failure_count_and_state():
    cb = CircuitBreaker("p", failure_threshold=3, reset_timeout_s=1)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.state == "closed"
    assert cb.failure_count == 0
    assert cb.last_failure_at is None


# --- state machine: open -----------------------------------------------------


async def test_open_rejects_requests_until_reset_timeout():
    cb = CircuitBreaker("p", failure_threshold=2, reset_timeout_s=60)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"
    assert await cb.allow() is False


# --- state machine: half_open -----------------------------------------------


async def test_open_transitions_to_half_open_after_timeout():
    cb = CircuitBreaker("p", failure_threshold=2, reset_timeout_s=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"

    # Within the reset window the breaker must still be open.
    assert await cb.allow() is False

    # After the reset window elapses the next allow() flips to half_open
    # and admits the probe.
    import asyncio

    await asyncio.sleep(0.08)
    assert await cb.allow() is True
    assert cb.state == "half_open"


async def test_half_open_success_closes_breaker():
    cb = CircuitBreaker("p", failure_threshold=2, reset_timeout_s=0.01)
    cb.record_failure()
    cb.record_failure()
    import asyncio

    await asyncio.sleep(0.02)
    # Probe admitted, transitions to half_open.
    assert await cb.allow() is True
    assert cb.state == "half_open"

    cb.record_success()
    assert cb.state == "closed"
    assert cb.failure_count == 0
    assert await cb.allow() is True


async def test_half_open_failure_reopens_breaker():
    cb = CircuitBreaker("p", failure_threshold=2, reset_timeout_s=0.01)
    cb.record_failure()
    cb.record_failure()
    import asyncio

    await asyncio.sleep(0.02)
    assert await cb.allow() is True  # half_open probe
    assert cb.state == "half_open"

    cb.record_failure()
    assert cb.state == "open"
    # And the breaker is rejecting again.
    assert await cb.allow() is False


# --- validation --------------------------------------------------------------


def test_failure_threshold_must_be_positive():
    with pytest.raises(ValueError):
        CircuitBreaker("p", failure_threshold=0, reset_timeout_s=1)


def test_reset_timeout_must_be_positive():
    with pytest.raises(ValueError):
        CircuitBreaker("p", failure_threshold=1, reset_timeout_s=0)


# --- module-level singleton ---------------------------------------------------


def test_get_breaker_returns_singleton_per_provider():
    a = get_breaker("provider-a", failure_threshold=3, reset_timeout_s=10)
    b = get_breaker("provider-a", failure_threshold=99, reset_timeout_s=999)
    assert a is b
    # The second call returns the existing instance; its constructor args
    # are ignored on purpose so different env-driven configs don't thrash
    # state mid-process.


def test_get_breaker_returns_distinct_per_provider():
    a = get_breaker("provider-a")
    b = get_breaker("provider-b")
    assert a is not b
    assert a.provider == "provider-a"
    assert b.provider == "provider-b"


# --- exceptions --------------------------------------------------------------


def test_circuit_open_carries_provider_and_retry_after():
    exc = CircuitOpen("p", retry_after_s=12.5)
    assert exc.provider == "p"
    assert exc.retry_after_s == 12.5
    assert "p" in str(exc)
    assert "12.500" in str(exc)


def test_all_providers_failed_is_an_exception():
    # AllProvidersFailed is raised by FallbackChain; here we just confirm
    # the type is importable and usable.
    with pytest.raises(AllProvidersFailed):
        raise AllProvidersFailed("boom")


# --- retry_after helper ------------------------------------------------------


def test_retry_after_zero_when_closed():
    cb = CircuitBreaker("p", failure_threshold=2, reset_timeout_s=10)
    assert cb.retry_after() == 0.0


def test_retry_after_decreases_as_time_passes(monkeypatch):
    cb = CircuitBreaker("p", failure_threshold=2, reset_timeout_s=10)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "open"

    # Pretend 4 seconds have passed since last_failure_at.
    base = cb.last_failure_at
    assert base is not None
    cb.last_failure_at = base - timedelta(seconds=4)
    assert cb.retry_after() == pytest.approx(6.0, abs=0.01)


def test_retry_after_zero_when_window_elapsed():
    cb = CircuitBreaker("p", failure_threshold=2, reset_timeout_s=1)
    cb.record_failure()
    cb.record_failure()
    base = cb.last_failure_at
    assert base is not None
    cb.last_failure_at = base - timedelta(seconds=2)
    assert cb.retry_after() == 0.0

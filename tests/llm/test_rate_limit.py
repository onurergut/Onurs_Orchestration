"""Tests for the per-provider Redis token-bucket rate limiter.

These tests use a dedicated Redis DB index (``db=2``) so production data
(default ``db=0``) is not touched.
"""

import os

import pytest
from redis.asyncio import Redis

from orchestration.llm.rate_limit import (
    RateLimitExceeded,
    TokenBucket,
    get_bucket,
    reset_buckets,
)

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/2")


def _make_client() -> Redis:
    return Redis.from_url(TEST_REDIS_URL, decode_responses=True)


async def _flush_test_keys(client: Redis, provider: str) -> None:
    await client.delete(f"ratelimit:{provider}")


@pytest.fixture
async def redis_client():
    client = _make_client()
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch):
    """Reset module-level singleton cache between tests so env changes apply."""
    reset_buckets()
    # Clear any provider-specific env vars that might leak across tests.
    for var in (
        "LLM_RATELIMIT_TEST_RPS",
        "LLM_RATELIMIT_TEST_CAPACITY",
        "LLM_RATELIMIT_ALPHA_RPS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    reset_buckets()


async def test_acquire_succeeds_when_tokens_available(redis_client):
    bucket = TokenBucket("acquire-ok", capacity=5, refill_per_second=1.0, redis_client=redis_client)
    await _flush_test_keys(redis_client, "acquire-ok")
    remaining = await bucket.acquire()
    assert remaining == pytest.approx(4.0, abs=1e-3)


async def test_acquire_raises_after_capacity_exhausted(redis_client):
    bucket = TokenBucket(
        "exhaust-me",
        capacity=3,
        refill_per_second=1.0,
        redis_client=redis_client,
    )
    await _flush_test_keys(redis_client, "exhaust-me")
    await bucket.acquire()
    await bucket.acquire()
    await bucket.acquire()
    with pytest.raises(RateLimitExceeded) as exc:
        await bucket.acquire()
    assert exc.value.provider == "exhaust-me"
    assert exc.value.current_tokens < 1.0
    assert exc.value.retry_after_s > 0


async def test_get_bucket_is_singleton_per_provider(redis_client):
    a = get_bucket("singleton-provider", redis_client=redis_client)
    b = get_bucket("singleton-provider", redis_client=redis_client)
    assert a is b


async def test_get_bucket_separate_per_provider(redis_client):
    a = get_bucket("provider-a", redis_client=redis_client)
    b = get_bucket("provider-b", redis_client=redis_client)
    assert a is not b
    assert a.provider == "provider-a"
    assert b.provider == "provider-b"


async def test_env_var_override_rps(monkeypatch, redis_client):
    monkeypatch.setenv("LLM_RATELIMIT_TEST_RPS", "10")
    bucket = get_bucket("test", redis_client=redis_client)
    assert bucket.refill_per_second == 10.0
    assert bucket.capacity == TokenBucket.DEFAULT_CAPACITY


async def test_env_var_override_capacity(monkeypatch, redis_client):
    monkeypatch.setenv("LLM_RATELIMIT_TEST_CAPACITY", "120")
    monkeypatch.setenv("LLM_RATELIMIT_TEST_RPS", "2")
    bucket = get_bucket("test", redis_client=redis_client)
    assert bucket.capacity == 120.0
    assert bucket.refill_per_second == 2.0


async def test_refill_over_time(redis_client):
    """Refill rate governs how fast tokens become available over time."""
    import asyncio

    # capacity=2, refill=10/sec. After draining to 0 and sleeping 0.15s
    # we should refill ~1.5 tokens, leaving ~0.5 after one acquire.
    bucket = TokenBucket(
        "refill-test",
        capacity=2,
        refill_per_second=10.0,
        redis_client=redis_client,
    )
    await _flush_test_keys(redis_client, "refill-test")
    await bucket.acquire()
    await bucket.acquire()
    with pytest.raises(RateLimitExceeded):
        await bucket.acquire()
    await asyncio.sleep(0.15)
    remaining = await bucket.acquire()
    assert remaining == pytest.approx(0.5, abs=0.3)


async def test_router_build_client_consumes_token(redis_client, monkeypatch):
    """build_client() should consume a token from the per-provider bucket."""
    from orchestration.llm.endpoint import LLMEndpoint
    from orchestration.llm.router import build_client

    monkeypatch.setenv("LLM_RATELIMIT_ROUTER_TEST_RPS", "1")
    monkeypatch.setenv("LLM_RATELIMIT_ROUTER_TEST_CAPACITY", "2")
    # Pre-clear singleton so env overrides take effect.
    reset_buckets()

    endpoint = LLMEndpoint(
        name="router-test",
        protocol="openai",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        model="qwen2.5-coder:7b",
        tier="FAST",
    )
    # Inject the test Redis client into the bucket the router will use.
    get_bucket("router-test", redis_client=redis_client)
    await _flush_test_keys(redis_client, "router-test")

    await build_client(endpoint)
    await build_client(endpoint)
    with pytest.raises(RateLimitExceeded):
        await build_client(endpoint)
"""Per-provider LLM rate limiter using a Redis-backed token bucket.

The bucket is keyed per provider (``ratelimit:{provider}``) and refilled at a
fixed rate. Every call to :meth:`TokenBucket.acquire` is performed atomically
through a Lua script so concurrent callers cannot over-consume the bucket.

This is a *fail-fast* implementation: when the bucket is empty, ``acquire``
raises :class:`RateLimitExceeded` instead of blocking. Callers are expected to
catch the exception and decide how to proceed (return a 429, surface an error,
or fall back to another provider).
"""

from __future__ import annotations

import os
import time
from typing import Any

from redis.asyncio import Redis

from orchestration.storage.redis import get_redis


class RateLimitExceeded(Exception):
    """Raised when a token bucket cannot fulfil an ``acquire`` request."""

    def __init__(self, provider: str, current_tokens: float, retry_after_s: float):
        self.provider = provider
        self.current_tokens = current_tokens
        self.retry_after_s = retry_after_s
        super().__init__(
            f"rate limit exceeded for provider '{provider}' "
            f"(tokens={current_tokens:.3f}, retry_after={retry_after_s:.3f}s)"
        )


# KEYS[1] = ratelimit:{provider}
# ARGV[1] = capacity (float)
# ARGV[2] = refill_per_second (float)
# ARGV[3] = now (epoch seconds, float)
# ARGV[4] = tokens requested (int)
# Returns: {allowed (0|1), remaining_tokens (float)}
_LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last')
local tokens = tonumber(data[1])
local last = tonumber(data[2])

if tokens == nil then
  tokens = capacity
  last = now
end

local elapsed = now - last
if elapsed < 0 then elapsed = 0 end
tokens = tokens + elapsed * refill
if tokens > capacity then tokens = capacity end

if tokens >= requested then
  tokens = tokens - requested
  redis.call('HMSET', key, 'tokens', tokens, 'last', now)
  -- Return the float as a string: Redis' EVAL protocol truncates Lua
  -- numbers to integers, so we round-trip via tostring().
  return {1, tostring(tokens)}
else
  redis.call('HMSET', key, 'tokens', tokens, 'last', now)
  return {0, tostring(tokens)}
end
"""


class TokenBucket:
    """Single-provider token bucket backed by a Redis hash + Lua script."""

    DEFAULT_CAPACITY = 60
    DEFAULT_REFILL_PER_SECOND = 1.0

    def __init__(
        self,
        provider: str,
        capacity: int = DEFAULT_CAPACITY,
        refill_per_second: float = DEFAULT_REFILL_PER_SECOND,
        redis_client: Redis | None = None,
    ):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        self.provider = provider
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._redis = redis_client
        self._key = f"ratelimit:{provider}"

    @staticmethod
    def _script() -> str:
        return _LUA_SCRIPT

    def _client(self) -> Redis:
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    async def acquire(self, tokens: int = 1) -> float:
        """Consume ``tokens`` from the bucket; raise if not enough are available."""
        if tokens <= 0:
            raise ValueError("tokens must be positive")
        client = self._client()
        now = time.time()
        result: Any = await client.eval(
            self._script(),
            1,
            self._key,
            str(self.capacity),
            str(self.refill_per_second),
            f"{now:.6f}",
            str(tokens),
        )
        allowed = int(result[0])
        # result[1] is returned via tostring() so floats survive the EVAL
        # protocol; decode in case the client returns bytes (no decode_responses).
        raw_current = result[1]
        if isinstance(raw_current, (bytes, bytearray)):
            raw_current = raw_current.decode()
        current = float(raw_current)
        if allowed == 0:
            missing = float(tokens) - current
            retry_after = missing / self.refill_per_second if missing > 0 else 0.0
            raise RateLimitExceeded(self.provider, current, retry_after)
        return current


_buckets: dict[str, TokenBucket] = {}


def _config_for_provider(provider: str) -> tuple[int, float]:
    """Read per-provider capacity/refill from ``LLM_RATELIMIT_<PROVIDER>_*`` env vars."""
    upper = provider.upper().replace("-", "_")
    cap_env = os.environ.get(f"LLM_RATELIMIT_{upper}_CAPACITY")
    rps_env = os.environ.get(f"LLM_RATELIMIT_{upper}_RPS")
    capacity = int(cap_env) if cap_env else TokenBucket.DEFAULT_CAPACITY
    refill = float(rps_env) if rps_env else TokenBucket.DEFAULT_REFILL_PER_SECOND
    return capacity, refill


def get_bucket(
    provider: str,
    redis_client: Redis | None = None,
) -> TokenBucket:
    """Return the module-level :class:`TokenBucket` singleton for ``provider``."""
    bucket = _buckets.get(provider)
    if bucket is None:
        capacity, refill = _config_for_provider(provider)
        bucket = TokenBucket(
            provider,
            capacity=capacity,
            refill_per_second=refill,
            redis_client=redis_client,
        )
        _buckets[provider] = bucket
    elif redis_client is not None and bucket._redis is None:
        bucket._redis = redis_client
    return bucket


def reset_buckets() -> None:
    """Clear the singleton cache (test helper)."""
    _buckets.clear()
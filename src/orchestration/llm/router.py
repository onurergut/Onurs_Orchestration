"""LLM client router.

Two entry points:

* :func:`build_client` — single-endpoint case. Reserves a token in the
  per-provider rate-limit bucket and returns an :class:`LLMClient` ready to
  serve a single request. Existing callers continue to use this.
* :func:`build_client_with_fallback` — resilience case. Returns a
  :class:`~orchestration.llm.fallback.FallbackChain` that walks a primary
  plus zero or more fallback endpoints, skipping any whose circuit breaker
  is open. New code that needs resilience should prefer this.

Both entry points share the same per-provider rate-limit bucket via
:func:`~orchestration.llm.rate_limit.get_bucket`.
"""

from __future__ import annotations

from orchestration.llm.anthropic_compat import AnthropicCompatClient
from orchestration.llm.client import LLMClient
from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.fallback import FallbackChain
from orchestration.llm.openai_compat import OpenAICompatClient
from orchestration.llm.rate_limit import get_bucket


async def build_client(endpoint: LLMEndpoint) -> LLMClient:
    # Per-provider global rate limit. Reserves a token before any LLM call
    # made through the returned client; raises RateLimitExceeded when the
    # provider's bucket is empty so callers can fail fast.
    await get_bucket(endpoint.name).acquire(1)
    if endpoint.protocol == "openai":
        return OpenAICompatClient(endpoint)
    return AnthropicCompatClient(endpoint)


def build_client_with_fallback(
    primary: LLMEndpoint,
    fallbacks: list[LLMEndpoint] | None = None,
) -> FallbackChain:
    """Return a :class:`FallbackChain` for ``primary`` with the given fallbacks.

    The chain tries the primary first, then each fallback in order. Calls
    against an endpoint whose circuit breaker is open are skipped; calls
    that raise are recorded as failures on the breaker and the chain moves
    on. A successful call resets the breaker for that provider.

    Callers should use this whenever they want a single ``complete()``
    request to transparently survive a transient provider outage. The
    returned object is reusable across requests — it only holds the
    endpoint configuration, not per-request state.
    """
    return FallbackChain(primary=primary, fallbacks=fallbacks)

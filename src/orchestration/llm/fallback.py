"""Provider fallback chain for the LLM layer.

A :class:`FallbackChain` wraps a primary endpoint and an ordered list of
fallback endpoints. Each call to :meth:`FallbackChain.complete` walks the
chain in order, skipping any endpoint whose circuit breaker is open and
attempting the next one whenever a call raises. As soon as a provider
returns a response, the breaker for that provider is marked healthy and the
response is returned to the caller.

If every provider fails (either by raising or by being skipped), the chain
raises :class:`~orchestration.llm.circuit_breaker.AllProvidersFailed`.

This module deliberately depends on the router for client construction so
that rate-limiting, retry policy, and protocol selection stay in one place.
"""

from __future__ import annotations

import logging

from orchestration.llm.circuit_breaker import (
    AllProvidersFailed,
    CircuitBreaker,
    get_breaker,
)
from orchestration.llm.client import CompletionRequest, CompletionResponse
from orchestration.llm.endpoint import LLMEndpoint

# NOTE: ``build_client`` is imported lazily inside :meth:`FallbackChain.complete`
# to avoid a circular import: ``orchestration.llm.router`` re-exports
# :class:`FallbackChain` via :func:`build_client_with_fallback`, so a top-level
# ``from orchestration.llm.router import build_client`` here would deadlock.

logger = logging.getLogger(__name__)


class FallbackChain:
    """Ordered list of endpoints tried in sequence until one succeeds.

    Parameters
    ----------
    primary:
        The first endpoint tried. Every chain must have exactly one primary.
    fallbacks:
        Zero or more fallback endpoints tried in order when the primary (or
        an earlier fallback) fails or is skipped because its breaker is open.
    breaker_for:
        Optional factory used to look up the :class:`CircuitBreaker` for a
        given endpoint. Defaults to the module-level ``get_breaker`` keyed
        by ``endpoint.name``. Tests inject a fresh factory to keep state
        isolated.
    """

    def __init__(
        self,
        primary: LLMEndpoint,
        fallbacks: list[LLMEndpoint] | None = None,
        *,
        breaker_for=get_breaker,
    ):
        if primary is None:
            raise ValueError("primary endpoint is required")
        self.primary = primary
        self.fallbacks: list[LLMEndpoint] = list(fallbacks or [])
        self._breaker_for = breaker_for

    def _endpoints(self) -> list[LLMEndpoint]:
        return [self.primary, *self.fallbacks]

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run ``request`` against the chain. Raise :class:`AllProvidersFailed` if every provider fails."""
        # Local import to avoid a circular import with orchestration.llm.router.
        from orchestration.llm.router import build_client

        last_error: Exception | None = None
        for endpoint in self._endpoints():
            breaker = self._breaker_for(endpoint.name)
            if not await breaker.allow():
                logger.warning(
                    "fallback.skip endpoint=%s reason=circuit_open", endpoint.name
                )
                continue
            try:
                client = await build_client(endpoint)
                response = await client.complete(request)
            except Exception as exc:  # noqa: BLE001 - want to surface any provider failure
                breaker.record_failure()
                last_error = exc
                logger.warning(
                    "fallback.provider_failed endpoint=%s error=%s",
                    endpoint.name,
                    exc,
                )
                continue
            breaker.record_success()
            return response
        raise AllProvidersFailed(
            f"all providers failed for request (tried={[e.name for e in self._endpoints()]}); "
            f"last_error={last_error!r}"
        ) from last_error


__all__ = ["AllProvidersFailed", "CircuitBreaker", "FallbackChain"]

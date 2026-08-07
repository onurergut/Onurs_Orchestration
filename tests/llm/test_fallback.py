"""Tests for the LLM fallback chain.

These tests avoid the real router / rate limiter by monkeypatching
``build_client`` inside :mod:`orchestration.llm.fallback` to return a
configurable stub client. The stub can be told to raise a chosen
exception or to return a chosen response per endpoint, so we can drive the
chain through every path (primary ok, primary fails, primary breaker open,
all fail) deterministically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from orchestration.llm import router as router_module
from orchestration.llm.circuit_breaker import (
    AllProvidersFailed,
    CircuitBreaker,
    reset_breakers,
)
from orchestration.llm.client import CompletionRequest, CompletionResponse
from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.fallback import FallbackChain

# --- test fixtures & helpers -------------------------------------------------


def _endpoint(name: str) -> LLMEndpoint:
    return LLMEndpoint(
        name=name,
        protocol="openai",
        base_url="http://localhost:11434/v1",
        api_key="test-key",
        model="test-model",
        tier="FAST",
    )


def _resp(endpoint_name: str, content: str = "ok") -> CompletionResponse:
    return CompletionResponse(
        content=content,
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=0.0,
        model="test-model",
        endpoint_name=endpoint_name,
    )


class StubClient:
    """Configurable stand-in for an :class:`LLMClient`."""

    def __init__(
        self,
        endpoint_name: str,
        behavior: Callable[[CompletionRequest], Awaitable[CompletionResponse]]
        | Exception,
    ):
        self.endpoint_name = endpoint_name
        self.calls = 0
        self._behavior = behavior

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        if isinstance(self._behavior, Exception):
            raise self._behavior
        return await self._behavior(request)


@pytest.fixture
def stub_build(monkeypatch):
    """Replace ``build_client`` inside the fallback module with a stub.

    Returns a tuple ``(installer, clients)`` where ``installer`` accepts a
    mapping ``endpoint_name -> behavior`` and patches ``build_client`` for
    the test, and ``clients`` is a list that captures the constructed
    ``StubClient`` objects in the order they were built.
    """
    captured: list[StubClient] = []

    def installer(behavior_map: dict[str, Callable | Exception]) -> Callable:
        async def fake_build_client(endpoint: LLMEndpoint):
            behavior = behavior_map.get(endpoint.name, _resp(endpoint.name))
            stub = StubClient(endpoint.name, behavior)
            captured.append(stub)
            return stub

        # ``FallbackChain.complete`` does a lazy ``from orchestration.llm.router
        # import build_client`` inside the method body, so monkeypatching on the
        # router module is what actually intercepts the call.
        monkeypatch.setattr(router_module, "build_client", fake_build_client)
        return fake_build_client

    yield installer, captured


@pytest.fixture(autouse=True)
def _reset_breakers():
    reset_breakers()
    yield
    reset_breakers()


@pytest.fixture
def request_obj() -> CompletionRequest:
    return CompletionRequest(messages=[{"role": "user", "content": "hi"}])


# --- happy path --------------------------------------------------------------


async def test_primary_success_returns_primary_response(stub_build, request_obj):
    primary = _endpoint("primary")
    fallback = _endpoint("fallback")
    installer, _ = stub_build

    async def ok(req: CompletionRequest) -> CompletionResponse:
        return _resp("primary", "from primary")

    async def fb_ok(req: CompletionRequest) -> CompletionResponse:
        return _resp("fallback", "fb")

    installer({"primary": ok, "fallback": fb_ok})

    chain = FallbackChain(primary=primary, fallbacks=[fallback])
    response = await chain.complete(request_obj)

    assert response.endpoint_name == "primary"
    assert response.content == "from primary"


async def test_no_fallbacks_works(stub_build, request_obj):
    primary = _endpoint("primary")
    installer, _ = stub_build

    async def ok(req: CompletionRequest) -> CompletionResponse:
        return _resp("primary")

    installer({"primary": ok})

    chain = FallbackChain(primary=primary)
    response = await chain.complete(request_obj)
    assert response.endpoint_name == "primary"


# --- failure paths -----------------------------------------------------------


async def test_primary_failure_falls_back_to_secondary(stub_build, request_obj):
    primary = _endpoint("primary")
    fb = _endpoint("fallback")
    installer, clients = stub_build

    async def fb_handler(req: CompletionRequest) -> CompletionResponse:
        return _resp("fallback", "from fallback")

    installer({"primary": RuntimeError("primary down"), "fallback": fb_handler})

    chain = FallbackChain(primary=primary, fallbacks=[fb])
    response = await chain.complete(request_obj)

    assert response.endpoint_name == "fallback"
    assert response.content == "from fallback"
    # Both endpoints were attempted in order.
    assert [c.endpoint_name for c in clients] == ["primary", "fallback"]


async def test_primary_circuit_open_skips_primary(stub_build, request_obj):
    primary = _endpoint("primary")
    fb = _endpoint("fallback")
    installer, clients = stub_build

    async def fb_handler(req: CompletionRequest) -> CompletionResponse:
        return _resp("fallback", "fb ok")

    installer({"primary": RuntimeError("never called"), "fallback": fb_handler})

    # Trip the primary breaker by recording enough failures.
    primary_breaker = CircuitBreaker("primary", failure_threshold=2, reset_timeout_s=60)
    primary_breaker.record_failure()
    primary_breaker.record_failure()
    assert primary_breaker.state == "open"

    def breaker_factory(name: str) -> CircuitBreaker:
        # Return our hand-built open breaker for "primary"; the fallback's
        # breaker comes from the real module-level singleton map (which is
        # reset by the autouse fixture).
        if name == "primary":
            return primary_breaker
        return CircuitBreaker(name)  # not cached, but each call returns fresh state

    chain = FallbackChain(
        primary=primary,
        fallbacks=[fb],
        breaker_for=breaker_factory,
    )
    response = await chain.complete(request_obj)

    assert response.endpoint_name == "fallback"
    # Primary was skipped entirely: only the fallback client was built.
    assert [c.endpoint_name for c in clients] == ["fallback"]


async def test_all_providers_fail_raises_all_providers_failed(stub_build, request_obj):
    primary = _endpoint("primary")
    fb = _endpoint("fallback")
    installer, _ = stub_build
    installer({"primary": RuntimeError("p broken"), "fallback": RuntimeError("fb broken")})

    chain = FallbackChain(primary=primary, fallbacks=[fb])
    with pytest.raises(AllProvidersFailed) as exc:
        await chain.complete(request_obj)

    # The most recent underlying error is chained via __cause__.
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert str(exc.value.__cause__) == "fb broken"


async def test_partial_failures_record_failure_counts(stub_build, request_obj):
    primary = _endpoint("primary")
    fb = _endpoint("fallback")
    installer, _ = stub_build

    primary_cb = CircuitBreaker("primary", failure_threshold=10, reset_timeout_s=60)
    fb_cb = CircuitBreaker("fallback", failure_threshold=10, reset_timeout_s=60)

    def breaker_for(name: str) -> CircuitBreaker:
        if name == "primary":
            return primary_cb
        if name == "fallback":
            return fb_cb
        raise AssertionError(f"unexpected provider {name}")

    async def raise_500(req: CompletionRequest) -> CompletionResponse:
        raise RuntimeError(f"{req.tier} failed")

    installer({"primary": raise_500, "fallback": raise_500})

    chain = FallbackChain(
        primary=primary,
        fallbacks=[fb],
        breaker_for=breaker_for,
    )
    with pytest.raises(AllProvidersFailed):
        await chain.complete(request_obj)

    # Both providers should have one recorded failure each.
    assert primary_cb.failure_count == 1
    assert fb_cb.failure_count == 1


async def test_success_resets_primary_failure_count(stub_build, request_obj):
    primary = _endpoint("primary")
    fb = _endpoint("fallback")
    installer, _ = stub_build

    primary_cb = CircuitBreaker("primary", failure_threshold=10, reset_timeout_s=60)
    fb_cb = CircuitBreaker("fallback", failure_threshold=10, reset_timeout_s=60)
    primary_cb.record_failure()
    primary_cb.record_failure()
    assert primary_cb.failure_count == 2

    async def ok(req: CompletionRequest) -> CompletionResponse:
        return _resp("primary", "ok now")

    installer({"primary": ok})

    def breaker_for(name: str) -> CircuitBreaker:
        if name == "primary":
            return primary_cb
        return fb_cb  # shouldn't be used

    chain = FallbackChain(
        primary=primary,
        fallbacks=[fb],
        breaker_for=breaker_for,
    )
    response = await chain.complete(request_obj)
    assert response.endpoint_name == "primary"
    assert primary_cb.failure_count == 0
    assert primary_cb.state == "closed"


# --- validation --------------------------------------------------------------


def test_primary_required():
    with pytest.raises(ValueError):
        FallbackChain(primary=None)  # type: ignore[arg-type]


def test_fallbacks_default_is_empty():
    primary = _endpoint("primary")
    chain = FallbackChain(primary=primary)
    assert chain.fallbacks == []

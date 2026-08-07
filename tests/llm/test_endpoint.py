import asyncio
import os

import pytest

from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.router import build_client


@pytest.fixture(autouse=True)
async def _reset_redis_pool():
    """build_client now touches Redis (rate-limit acquire). The shared
    connection pool binds to the first event loop it sees, so we tear it
    down between tests to keep pytest-asyncio's per-function loop scope
    working. yield before teardown so the test itself runs cleanly first."""
    yield
    from orchestration.storage import redis as redis_mod

    pool = redis_mod._pool
    if pool is not None:
        try:
            await asyncio.shield(pool.disconnect())
        except RuntimeError:
            # Event loop may already be closing during teardown.
            pass
    redis_mod._pool = None
    from orchestration.llm import rate_limit

    rate_limit.reset_buckets()


async def test_openai_client_built():
    """Smoke: OpenAI-compatible client constructs with example config."""
    e = LLMEndpoint(
        name="primary",
        protocol="openai",
        base_url="http://localhost:11434/v1",  # example: Ollama default
        api_key="ollama",
        model="qwen2.5-coder:7b",
        tier="FAST",
    )
    c = await build_client(e)
    assert c.endpoint_name == "primary"


async def test_anthropic_client_built():
    """Smoke: Anthropic-compatible client constructs from env (LLM_* vars)."""
    e = LLMEndpoint(
        name="user-provider",
        protocol="anthropic",
        base_url=os.environ.get("LLM_ENDPOINT_URL", "https://api.anthropic.com"),
        api_key=os.environ.get("LLM_API_KEY", "sk-test"),
        model=os.environ.get("LLM_MODEL", "claude-sonnet-4-5"),
        tier="THINKING",
    )
    c = await build_client(e)
    assert c.endpoint_name == "user-provider"
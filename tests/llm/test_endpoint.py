import os

import pytest

from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.router import build_client


@pytest.fixture(autouse=True)
async def _reset_redis_pool():
    """Tear down Redis pool + rate-limit buckets between tests."""
    yield
    try:
        from orchestration.storage.redis import disconnect_pool

        disconnect_pool()
        from orchestration.llm import rate_limit

        rate_limit.reset_buckets()
    except Exception as _exc:
        import logging

        logging.getLogger(__name__).debug("ignoring fixture teardown error", exc_info=True)


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
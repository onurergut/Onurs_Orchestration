import pytest


@pytest.mark.asyncio
async def test_m2_end_to_end_with_user_llm(tmp_path):
    """Requires LLM_* env vars set and a reachable endpoint (Anthropic-compatible)."""
    pytest.skip("requires live LLM endpoint + git repo; run manually: `make e2e-m2`")
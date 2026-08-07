from orchestration.llm.anthropic_compat import AnthropicCompatClient
from orchestration.llm.client import LLMClient
from orchestration.llm.endpoint import LLMEndpoint
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
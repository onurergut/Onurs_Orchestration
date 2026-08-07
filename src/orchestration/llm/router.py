from orchestration.llm.anthropic_compat import AnthropicCompatClient
from orchestration.llm.client import LLMClient
from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.openai_compat import OpenAICompatClient


def build_client(endpoint: LLMEndpoint) -> LLMClient:
    if endpoint.protocol == "openai":
        return OpenAICompatClient(endpoint)
    return AnthropicCompatClient(endpoint)

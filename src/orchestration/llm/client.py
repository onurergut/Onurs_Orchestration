from typing import Protocol

from pydantic import BaseModel


class CompletionRequest(BaseModel):
    messages: list[dict]
    tier: str = "FAST"
    max_tokens: int = 4096
    temperature: float = 0.0


class CompletionResponse(BaseModel):
    content: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    model: str
    endpoint_name: str


class LLMClient(Protocol):
    @property
    def endpoint_name(self) -> str: ...

    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

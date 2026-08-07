import httpx

from orchestration.llm.client import CompletionRequest, CompletionResponse
from orchestration.llm.endpoint import LLMEndpoint
from orchestration.observability.tracing import tracer


class OpenAICompatClient:
    def __init__(self, endpoint: LLMEndpoint):
        self.endpoint = endpoint

    @property
    def endpoint_name(self) -> str:
        return self.endpoint.name

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        with tracer.start_as_current_span("llm.complete") as span:
            span.set_attribute("llm.endpoint", self.endpoint.name)
            span.set_attribute("llm.model", self.endpoint.model)
            span.set_attribute("llm.tier", self.endpoint.tier)
            async with httpx.AsyncClient(base_url=self.endpoint.base_url) as http:
                r = await http.post(
                    "/chat/completions",
                    headers={"Authorization": f"Bearer {self.endpoint.api_key.get_secret_value()}"},
                    json={
                        "model": self.endpoint.model,
                        "messages": request.messages,
                        "max_tokens": request.max_tokens,
                        "temperature": request.temperature,
                    },
                )
                r.raise_for_status()
                data = r.json()
            choice = data["choices"][0]
            usage = data.get("usage", {})
            return CompletionResponse(
                content=choice["message"]["content"],
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                cost_usd=0.0,  # computed by router from pricing table
                model=self.endpoint.model,
                endpoint_name=self.endpoint.name,
            )
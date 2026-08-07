import httpx

from orchestration.llm.client import CompletionRequest, CompletionResponse
from orchestration.llm.endpoint import LLMEndpoint
from orchestration.observability.tracing import tracer


class AnthropicCompatClient:
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
            # Extract system message separately (Anthropic API quirk)
            system = next((m["content"] for m in request.messages if m["role"] == "system"), None)
            messages = [m for m in request.messages if m["role"] != "system"]

            async with httpx.AsyncClient(base_url=self.endpoint.base_url) as http:
                r = await http.post(
                    "/v1/messages",
                    headers={
                        "x-api-key": self.endpoint.api_key.get_secret_value(),
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": self.endpoint.model,
                        "messages": messages,
                        "max_tokens": request.max_tokens,
                        "system": system,
                    },
                )
                r.raise_for_status()
                data = r.json()

            usage = data.get("usage", {})
            return CompletionResponse(
                content=data["content"][0]["text"],
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
                cost_usd=0.0,
                model=self.endpoint.model,
                endpoint_name=self.endpoint.name,
            )
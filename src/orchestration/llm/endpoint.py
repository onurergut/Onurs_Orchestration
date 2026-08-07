from typing import Literal

from pydantic import BaseModel, SecretStr


class LLMEndpoint(BaseModel):
    name: str
    protocol: Literal["openai", "anthropic"]
    base_url: str
    api_key: SecretStr
    model: str
    tier: Literal["FAST", "THINKING"]

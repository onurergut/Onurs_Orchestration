from abc import ABC, abstractmethod
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from orchestration.llm.client import LLMClient


class StageContext(BaseModel):
    run_id: UUID
    project_id: UUID
    task_external_id: str
    task_title: str
    task_description: str | None
    worktree_path: Path
    previous_artifacts: dict


class StageCrew(ABC):
    name: str
    output_schema: type[BaseModel]

    @abstractmethod
    async def run(self, ctx: StageContext, llm: LLMClient) -> BaseModel: ...

from abc import ABC, abstractmethod
from uuid import UUID

from pydantic import BaseModel


class ExternalTask(BaseModel):
    external_id: str
    title: str
    description: str | None = None
    metadata: dict = {}


class CreateTask(BaseModel):
    title: str
    description: str
    metadata: dict = {}


class Comment(BaseModel):
    external_id: str
    body: str


class TaskSourceAdapter(ABC):
    name: str

    @abstractmethod
    async def list_tasks(self, project_id: UUID, status: str | None) -> list[ExternalTask]: ...

    @abstractmethod
    async def get_task(self, task_id: str) -> ExternalTask: ...

    @abstractmethod
    async def create_task(self, project_id: UUID, payload: CreateTask) -> ExternalTask: ...

    @abstractmethod
    async def update_status(self, task_id: str, status: str) -> None: ...

    @abstractmethod
    async def add_comment(self, task_id: str, body: str) -> Comment: ...

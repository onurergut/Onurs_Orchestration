from abc import ABC, abstractmethod
from uuid import UUID

from pydantic import BaseModel


class Artifacts(BaseModel):
    summary: str
    diff: str | None = None
    test_report: dict | None = None


class Decision(BaseModel):
    approve: bool
    reject: bool = False
    comment: str | None = None
    decided_by: str | None = None


class Receipt(BaseModel):
    id: UUID
    external_id: str | None = None


class ReviewSurfaceAdapter(ABC):
    name: str

    @abstractmethod
    async def notify_review(self, run_id: UUID, artifacts: Artifacts) -> Receipt: ...

    @abstractmethod
    async def await_decision(self, receipt: Receipt, timeout_s: int) -> Decision: ...

    @abstractmethod
    async def post_comment(self, run_id: UUID, body: str) -> None: ...

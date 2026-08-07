from pydantic import BaseModel
from uuid import UUID


class OrchestratorState(BaseModel):
    run_id: UUID
    flow_uuid: UUID | None = None
    project_id: UUID
    task_id: UUID
    task_external_id: str = ""
    task_title: str = ""
    task_description: str | None = None
    worktree_path: str = ""
    completed_stages: list[str] = []
    stage_artifacts: dict = {}
    external_receipts: dict = {}
    decision: dict | None = None
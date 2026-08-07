from crewai.flow.flow import Flow, listen, start
from pathlib import Path
from sqlalchemy import select

from orchestration.adapters.review.web_ui import WebUiAdapter
from orchestration.adapters.task_source.csv import CsvAdapter
from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.router import build_client
from orchestration.orchestrator.budget_guard import check as budget_check
from orchestration.orchestrator.state import OrchestratorState
from orchestration.stages.coder import CoderStage
from orchestration.stages.planner import PlannerStage
from orchestration.storage.db import session_scope
from orchestration.storage.models.run import Run
from orchestration.storage.models.task import Task
from orchestration.storage.repositories import audit as audit_repo
from orchestration.git.worktree import WorktreeManager


class OrchestratorFlow(Flow[OrchestratorState]):
    @start()
    async def load_context(self):
        # Hydrate state from Run + Task rows
        async with session_scope() as session:
            run = (await session.execute(
                select(Run).where(Run.id == self.state.run_id)
            )).scalar_one()
            task = (await session.execute(
                select(Task).where(Task.id == run.task_id)
            )).scalar_one()
            self.state.project_id = run.project_id
            self.state.task_id = run.task_id
            self.state.task_external_id = task.external_id
            self.state.task_title = task.title
            self.state.task_description = task.description
        await audit_repo.audit(self.state.project_id, self.state.run_id, "system:orchestrator", "load_context", {})

    @listen(load_context)
    async def planner_stage(self):
        await budget_check(self.state.project_id)
        if "planner" in self.state.completed_stages:
            return
        endpoint = LLMEndpoint(
            name="primary",
            protocol=os.environ.get("LLM_PROTOCOL", "anthropic"),
            base_url=os.environ.get("LLM_ENDPOINT_URL", "https://api.anthropic.com"),
            api_key=os.environ.get("LLM_API_KEY", "sk-test"),
            model=os.environ.get("LLM_MODEL", "claude-sonnet-4-5"),
            tier="THINKING",
        )
        llm = build_client(endpoint)
        ctx = type("Ctx", (), {
            "run_id": self.state.run_id,
            "project_id": self.state.project_id,
            "task_external_id": self.state.task_external_id,
            "task_title": self.state.task_title,
            "task_description": self.state.task_description,
            "worktree_path": Path(self.state.worktree_path),
            "previous_artifacts": {},
        })()
        plan = await PlannerStage().run(ctx, llm)
        self.state.stage_artifacts["planner"] = plan.model_dump()
        self.state.completed_stages.append("planner")

    @listen(planner_stage)
    async def coder_stage(self):
        await budget_check(self.state.project_id)
        if "coder" in self.state.completed_stages:
            return
        endpoint = LLMEndpoint(
            name="primary",
            protocol=os.environ.get("LLM_PROTOCOL", "anthropic"),
            base_url=os.environ.get("LLM_ENDPOINT_URL", "https://api.anthropic.com"),
            api_key=os.environ.get("LLM_API_KEY", "sk-test"),
            model=os.environ.get("LLM_MODEL", "claude-sonnet-4-5"),
            tier="FAST",
        )
        llm = build_client(endpoint)
        ctx = type("Ctx", (), {
            "run_id": self.state.run_id,
            "project_id": self.state.project_id,
            "task_external_id": self.state.task_external_id,
            "task_title": self.state.task_title,
            "task_description": self.state.task_description,
            "worktree_path": Path(self.state.worktree_path),
            "previous_artifacts": self.state.stage_artifacts,
        })()
        code = await CoderStage().run(ctx, llm)
        self.state.stage_artifacts["coder"] = code.model_dump()
        self.state.completed_stages.append("coder")
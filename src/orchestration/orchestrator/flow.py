import os
import time
from pathlib import Path

from crewai.flow.flow import Flow, and_, listen, start
from sqlalchemy import select

from orchestration.llm.endpoint import LLMEndpoint
from orchestration.llm.router import build_client
from orchestration.observability.audit_middleware import stage_audit
from orchestration.observability.metrics import observe_stage_duration
from orchestration.orchestrator.budget_guard import check as budget_check
from orchestration.orchestrator.state import OrchestratorState
from orchestration.stages.coder import CoderStage
from orchestration.stages.docs import DocsStage
from orchestration.stages.planner import PlannerStage
from orchestration.stages.reviewer import ReviewerStage
from orchestration.stages.security import SecurityStage
from orchestration.stages.tester import TesterStage
from orchestration.storage.db import session_scope
from orchestration.storage.models.run import Run
from orchestration.storage.models.task import Task
from orchestration.storage.repositories import audit as audit_repo


def _record_stage_duration(stage: str, start: float) -> None:
    """Emit the Prometheus stage_duration_seconds observation.

    Wraps the ``time.monotonic() - start`` math so every stage body
    uses an identical call site — the histogram label is the same
    name as the stage method, matching the spec.
    """
    observe_stage_duration(stage, time.monotonic() - start)


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
            # Resume mitigation (Task 20 review finding): hydrate the flow's
            # completed_stages from the Run row so already-done stages
            # short-circuit via their "stage in completed_stages" guards.
            # Run.completed_stages is JSONB (list[str] | None on the wire;
            # SQLAlchemy returns None for a NULL column or an empty list
            # for the [] default). Normalize both to an empty list before
            # handing it to the state model.
            self.state.completed_stages = (
                list(run.completed_stages) if run.completed_stages else []
            )
        async with stage_audit(self.state.project_id, self.state.run_id, "load_context"):
            await audit_repo.audit(self.state.project_id, self.state.run_id, "system:orchestrator", "load_context", {})

    @listen(load_context)
    async def planner_stage(self):
        await budget_check(self.state.project_id)
        if "planner" in self.state.completed_stages:
            return
        async with stage_audit(self.state.project_id, self.state.run_id, "planner"):
            endpoint = LLMEndpoint(
                name="primary",
                protocol=os.environ.get("LLM_PROTOCOL", "anthropic"),
                base_url=os.environ.get("LLM_ENDPOINT_URL", "https://api.anthropic.com"),
                api_key=os.environ.get("LLM_API_KEY", "sk-test"),
                model=os.environ.get("LLM_MODEL", "claude-sonnet-4-5"),
                tier="THINKING",
            )
            llm = await build_client(endpoint)
            ctx = type("Ctx", (), {
                "run_id": self.state.run_id,
                "project_id": self.state.project_id,
                "task_external_id": self.state.task_external_id,
                "task_title": self.state.task_title,
                "task_description": self.state.task_description,
                "worktree_path": Path(self.state.worktree_path),
                "previous_artifacts": {},
            })()
            start = time.monotonic()
            plan = await PlannerStage().run(ctx, llm)
            _record_stage_duration("planner", start)
            self.state.stage_artifacts["planner"] = plan.model_dump()
            self.state.completed_stages.append("planner")

    @listen(planner_stage)
    async def coder_stage(self):
        await budget_check(self.state.project_id)
        if "coder" in self.state.completed_stages:
            return
        async with stage_audit(self.state.project_id, self.state.run_id, "coder"):
            endpoint = LLMEndpoint(
                name="primary",
                protocol=os.environ.get("LLM_PROTOCOL", "anthropic"),
                base_url=os.environ.get("LLM_ENDPOINT_URL", "https://api.anthropic.com"),
                api_key=os.environ.get("LLM_API_KEY", "sk-test"),
                model=os.environ.get("LLM_MODEL", "claude-sonnet-4-5"),
                tier="FAST",
            )
            llm = await build_client(endpoint)
            ctx = type("Ctx", (), {
                "run_id": self.state.run_id,
                "project_id": self.state.project_id,
                "task_external_id": self.state.task_external_id,
                "task_title": self.state.task_title,
                "task_description": self.state.task_description,
                "worktree_path": Path(self.state.worktree_path),
                "previous_artifacts": self.state.stage_artifacts,
            })()
            start = time.monotonic()
            code = await CoderStage().run(ctx, llm)
            _record_stage_duration("coder", start)
            self.state.stage_artifacts["coder"] = code.model_dump()
            self.state.completed_stages.append("coder")

    # ----- Parallel join: tester + security fan out from coder -----
    # Both branches carry the same decorator (``@listen(and_(coder_stage))``)
    # so the runtime schedules them as siblings triggered by a common
    # upstream completion. They share self.state but each only writes its
    # own artifact key, so concurrent writes are disjoint.
    @listen(and_(coder_stage))
    async def tester_stage(self):
        await budget_check(self.state.project_id)
        if "tester" in self.state.completed_stages:
            return
        async with stage_audit(self.state.project_id, self.state.run_id, "tester"):
            # Subprocess-only stage; pass llm=None since the protocol requires
            # the argument but TesterStage.run ignores it.
            ctx = type("Ctx", (), {
                "run_id": self.state.run_id,
                "project_id": self.state.project_id,
                "task_external_id": self.state.task_external_id,
                "task_title": self.state.task_title,
                "task_description": self.state.task_description,
                "worktree_path": Path(self.state.worktree_path),
                "previous_artifacts": self.state.stage_artifacts,
            })()
            start = time.monotonic()
            report = await TesterStage().run(ctx, llm=None)
            _record_stage_duration("tester", start)
            self.state.stage_artifacts["tester"] = report.model_dump()
            self.state.completed_stages.append("tester")

    @listen(and_(coder_stage))
    async def security_stage(self):
        await budget_check(self.state.project_id)
        if "security" in self.state.completed_stages:
            return
        async with stage_audit(self.state.project_id, self.state.run_id, "security"):
            ctx = type("Ctx", (), {
                "run_id": self.state.run_id,
                "project_id": self.state.project_id,
                "task_external_id": self.state.task_external_id,
                "task_title": self.state.task_title,
                "task_description": self.state.task_description,
                "worktree_path": Path(self.state.worktree_path),
                "previous_artifacts": self.state.stage_artifacts,
            })()
            start = time.monotonic()
            report = await SecurityStage().run(ctx, llm=None)
            _record_stage_duration("security", start)
            self.state.stage_artifacts["security"] = report.model_dump()
            self.state.completed_stages.append("security")

    # ----- Reviewer joins both parallel branches -----
    @listen(and_(tester_stage, security_stage))
    async def reviewer_stage(self):
        await budget_check(self.state.project_id)
        if "reviewer" in self.state.completed_stages:
            return
        async with stage_audit(self.state.project_id, self.state.run_id, "reviewer"):
            endpoint = LLMEndpoint(
                name="primary",
                protocol=os.environ.get("LLM_PROTOCOL", "anthropic"),
                base_url=os.environ.get("LLM_ENDPOINT_URL", "https://api.anthropic.com"),
                api_key=os.environ.get("LLM_API_KEY", "sk-test"),
                model=os.environ.get("LLM_MODEL", "claude-sonnet-4-5"),
                tier="THINKING",
            )
            llm = await build_client(endpoint)
            ctx = type("Ctx", (), {
                "run_id": self.state.run_id,
                "project_id": self.state.project_id,
                "task_external_id": self.state.task_external_id,
                "task_title": self.state.task_title,
                "task_description": self.state.task_description,
                "worktree_path": Path(self.state.worktree_path),
                "previous_artifacts": self.state.stage_artifacts,
            })()
            start = time.monotonic()
            review = await ReviewerStage().run(ctx, llm)
            _record_stage_duration("reviewer", start)
            self.state.stage_artifacts["reviewer"] = review.model_dump()
            self.state.completed_stages.append("reviewer")

    @listen(reviewer_stage)
    async def docs_stage(self):
        await budget_check(self.state.project_id)
        if "docs" in self.state.completed_stages:
            return
        async with stage_audit(self.state.project_id, self.state.run_id, "docs"):
            endpoint = LLMEndpoint(
                name="primary",
                protocol=os.environ.get("LLM_PROTOCOL", "anthropic"),
                base_url=os.environ.get("LLM_ENDPOINT_URL", "https://api.anthropic.com"),
                api_key=os.environ.get("LLM_API_KEY", "sk-test"),
                model=os.environ.get("LLM_MODEL", "claude-sonnet-4-5"),
                tier="THINKING",
            )
            llm = await build_client(endpoint)
            ctx = type("Ctx", (), {
                "run_id": self.state.run_id,
                "project_id": self.state.project_id,
                "task_external_id": self.state.task_external_id,
                "task_title": self.state.task_title,
                "task_description": self.state.task_description,
                "worktree_path": Path(self.state.worktree_path),
                "previous_artifacts": self.state.stage_artifacts,
            })()
            start = time.monotonic()
            update = await DocsStage().run(ctx, llm)
            _record_stage_duration("docs", start)
            self.state.stage_artifacts["docs"] = update.model_dump()
            self.state.completed_stages.append("docs")
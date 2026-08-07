import os

from crewai import LLM, Agent, Crew, Task
from pydantic import BaseModel

from orchestration.llm.client import LLMClient
from orchestration.stages.base import StageContext, StageCrew


def _build_crewai_llm() -> LLM:
    """Build a CrewAI LLM from LLM_* env vars.

    CrewAI's native Anthropic provider requires `crewai[anthropic]` extra.
    When that's missing we fall back to provider="openai".
    """
    protocol = os.environ.get("LLM_PROTOCOL", "anthropic").lower()
    model = os.environ["LLM_MODEL"]
    api_key = os.environ["LLM_API_KEY"]
    base_url = os.environ.get("LLM_ENDPOINT_URL")
    if protocol == "anthropic":
        try:
            return LLM(model=model, api_key=api_key, base_url=base_url, provider="anthropic")  # type: ignore[arg-type]
        except ImportError:
            pass
    return LLM(model=model, api_key=api_key, base_url=base_url, provider="openai")  # type: ignore[arg-type]


class Plan(BaseModel):
    files: list[str]
    steps: list[str]
    risks: list[str]


class PlannerStage(StageCrew):
    name = "planner"
    output_schema = Plan

    def __init__(self):
        self.llm = _build_crewai_llm()
        self.agent = Agent(
            role="Planner",
            goal="Produce a plan with files, steps, and risks for the given task",
            backstory="You are a meticulous software planner. Read the codebase and break the task into concrete steps.",
            allow_delegation=False,
            llm=self.llm,
        )

    async def run(self, ctx: StageContext, llm: LLMClient) -> Plan:
        task = Task(
            description=(
                f"Task: {ctx.task_title}\n{ctx.task_description or ''}\n\n"
                f"Worktree: {ctx.worktree_path}\n"
                "Produce a Plan JSON: {files: string[], steps: string[], risks: string[]}"
            ),
            expected_output="Plan JSON",
            agent=self.agent,
            output_pydantic=Plan,
        )
        crew = Crew(agents=[self.agent], tasks=[task])
        # CrewAI's Crew.kickoff() is synchronous and cannot be called from a
        # running event loop. PlannerStage.run is async, so use kickoff_async.
        if hasattr(crew, "kickoff_async"):
            result = await crew.kickoff_async()
        else:
            result = crew.kickoff()
        return Plan.model_validate(result.pydantic)  # type: ignore[union-attr]

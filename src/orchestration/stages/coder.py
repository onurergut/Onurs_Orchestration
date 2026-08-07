from pathlib import Path

from pydantic import BaseModel
from crewai import Agent, Crew, Task

from orchestration.llm.client import LLMClient
from orchestration.stages.base import StageContext, StageCrew


class CodeChange(BaseModel):
    diff_summary: str
    files_touched: list[str]
    test_files: list[str] = []


class CoderStage(StageCrew):
    name = "coder"
    output_schema = CodeChange

    def __init__(self):
        self.agent = Agent(
            role="Coder",
            goal="Implement the plan in the worktree",
            backstory="You write clean, minimal code. Always run tests after.",
            allow_delegation=False,
        )

    async def run(self, ctx: StageContext, llm: LLMClient) -> CodeChange:
        plan = ctx.previous_artifacts.get("planner", {})
        task = Task(
            description=(
                f"Implement this plan:\n{plan}\n\n"
                f"Worktree: {ctx.worktree_path}. Modify files, run tests, commit. "
                "Return CodeChange JSON."
            ),
            expected_output="CodeChange JSON",
            agent=self.agent,
            output_pydantic=CodeChange,
        )
        crew = Crew(agents=[self.agent], tasks=[task])
        result = crew.kickoff()
        return CodeChange.model_validate(result.pydantic)

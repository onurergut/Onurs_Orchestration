"""Suggestion engine.

Analyses a project's repository (README + tree + recent files) together with the
recent backlog pulled from a :class:`TaskSourceAdapter` and asks a THINKING-tier
LLM to propose new, valuable work. The resulting proposals are persisted as a
single ``Suggestion`` row in the database with ``status='pending'``.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from crewai import Agent, Crew, Task
from pydantic import BaseModel

from orchestration.adapters.task_source.base import ExternalTask, TaskSourceAdapter
from orchestration.git.worktree import WorktreeManager
from orchestration.llm.client import LLMClient
from orchestration.stages.planner import _build_crewai_llm
from orchestration.storage.db import session_scope
from orchestration.storage.models.project import Project
from orchestration.storage.models.suggestion import Suggestion


class Proposal(BaseModel):
    """A single proposal for new work."""

    title: str
    description: str
    rationale: str
    estimated_complexity: Literal["small", "medium", "large"]
    related_files: list[str] = []


class _ProposalList(BaseModel):
    """CrewAI's ``output_pydantic`` requires a BaseModel — wrap a list[Proposal]."""

    proposals: list[Proposal]


class SuggestionEngine:
    """Proposes new work for a project based on repo context + recent tasks."""

    def __init__(
        self,
        llm: LLMClient,
        worktree_manager: WorktreeManager,
        task_source: TaskSourceAdapter,
    ) -> None:
        self.llm = llm
        self.worktree_manager = worktree_manager
        self.task_source = task_source

    async def suggest(self, project_id: UUID) -> list[Proposal]:
        """Run the suggestion pipeline for ``project_id``.

        Steps:
          1. Load the project (repo_url, default_branch).
          2. Clone the repo into a temp directory.
          3. Build a context summary (top-level tree, README, recent files).
          4. Pull recent pending tasks from the task source.
          5. Ask the THINKING-tier LLM for 1-5 ``Proposal`` objects.
          6. Persist them as a single ``Suggestion`` row.

        Returns the list of proposals that was persisted.
        """
        project = await self._load_project(project_id)
        repo_path = await self._clone_to_temp(project.repo_url)
        try:
            context = self._read_repo_context(repo_path)
            recent = await self.task_source.list_tasks(project_id, status="pending")
            proposals = await self._analyze(project, context, recent)
            await self._persist(project_id, proposals)
            return proposals
        finally:
            # The clone lives in a fresh mkdtemp; remove the whole tree.
            shutil.rmtree(repo_path.parent, ignore_errors=True)

    async def _load_project(self, project_id: UUID) -> Project:
        async with session_scope() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise ValueError(f"Project {project_id} not found")
            return project

    async def _clone_to_temp(self, repo_url: str) -> Path:
        """Clone ``repo_url`` into a fresh temp directory; return the repo path."""
        temp_base = Path(tempfile.mkdtemp(prefix="orch-suggest-"))
        repo_dir = temp_base / "repo"
        await asyncio.to_thread(self._git_clone, repo_url, repo_dir)
        return repo_dir

    @staticmethod
    def _git_clone(url: str, dest: Path) -> None:
        subprocess.run(
            ["git", "clone", "--depth", "50", str(url), str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )

    def _read_repo_context(self, repo_dir: Path) -> str:
        """Build a string summary of the repo: top-level tree + README + recent files."""
        sections: list[str] = []

        top_level = sorted(
            p.name for p in repo_dir.iterdir() if not p.name.startswith(".git")
        )
        sections.append("Top-level entries: " + ", ".join(top_level[:50]))

        readme = repo_dir / "README.md"
        if readme.exists():
            content = readme.read_text(errors="ignore")[:4000]
            sections.append(f"README.md (truncated to 4000 chars):\n{content}")
        else:
            sections.append("(no README.md present)")

        recent = self._recent_files(repo_dir, limit=5)
        if recent:
            lines = ["5 most recently modified files (by git history):"]
            lines.extend(f"  - {rel}" for rel in recent)
            sections.append("\n".join(lines))
        else:
            sections.append("(no recent file history available)")

        return "\n\n".join(sections)

    @staticmethod
    def _recent_files(repo_dir: Path, limit: int = 5) -> list[str]:
        """Return up to ``limit`` most-recently-touched paths (de-duplicated, in order)."""
        r = subprocess.run(
            ["git", "log", "--name-only", "--pretty=format:", "-n", "50"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        seen: set[str] = set()
        ordered: list[str] = []
        for line in r.stdout.splitlines():
            stripped = line.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                ordered.append(stripped)
                if len(ordered) >= limit:
                    break
        return ordered

    async def _analyze(
        self,
        project: Project,
        context: str,
        recent_tasks: list[ExternalTask],
    ) -> list[Proposal]:
        """Prompt the THINKING-tier LLM with repo context + recent tasks."""
        task_lines = "\n".join(f"- {t.title}" for t in recent_tasks[:20]) or "- (none)"
        prompt = (
            "You are a senior engineer proposing valuable next work for the project "
            f"named '{project.name}' (default branch: {project.default_branch}).\n\n"
            f"## Repository context\n{context}\n\n"
            f"## Recent pending task titles\n{task_lines}\n\n"
            "Return a JSON object with a single key 'proposals' whose value is an "
            "array (length 1-5) of objects. Each object MUST include exactly these "
            "fields:\n"
            "  - title (short, action-oriented string)\n"
            "  - description (1-3 sentence summary)\n"
            "  - rationale (1-2 sentences explaining why this work is valuable now)\n"
            "  - estimated_complexity (one of: 'small', 'medium', 'large')\n"
            "  - related_files (array of repo-relative paths, may be empty)\n"
        )

        crew_llm = _build_crewai_llm()
        agent = Agent(
            role="SuggestionEngine",
            goal="Propose the highest-leverage next work for this repository",
            backstory=(
                "You are a thoughtful senior engineer who reads code, considers the "
                "team's existing backlog, and proposes focused, valuable next steps."
            ),
            allow_delegation=False,
            llm=crew_llm,
        )
        task = Task(
            description=prompt,
            expected_output="_ProposalList JSON with a 'proposals' array",
            agent=agent,
            output_pydantic=_ProposalList,
        )
        crew = Crew(agents=[agent], tasks=[task])
        # CrewAI's Crew.kickoff() is synchronous; kickoff_async exists on newer versions.
        if hasattr(crew, "kickoff_async"):
            result = await crew.kickoff_async()
        else:
            result = crew.kickoff()

        # ``result.pydantic`` is the BaseModel instance populated by ``output_pydantic``.
        # CrewAI returns CrewOutput | CrewStreamingOutput; only the former has `.pydantic`.
        pydantic_obj = getattr(result, "pydantic", None)
        wrapper = _ProposalList.model_validate(cast(BaseModel, pydantic_obj))
        return wrapper.proposals

    async def _persist(self, project_id: UUID, proposals: list[Proposal]) -> None:
        async with session_scope() as session:
            row = Suggestion(
                project_id=project_id,
                status="pending",
                proposals=[p.model_dump() for p in proposals],
            )
            session.add(row)
"""Docs stage: LLM proposes documentation updates for a git diff and commits them.

This stage runs a single `git diff main..HEAD --stat` and asks the LLM to
propose which docs files should be added (new) or updated (existing). The
LLM does the actual file writes via its own tooling — the stage only sees
the returned paths.

At the end of the run the stage makes a single commit
(`docs: update for <task_title>`) covering the LLM-proposed paths. Empty
proposals short-circuit before the commit, so the pipeline is never
burdened with an empty `git commit`.

This is v1: the stage trusts the LLM's path output as ground truth. A
future iteration could read each path back and verify the file exists
and is non-empty.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from crewai import Agent, Crew, Task
from pydantic import BaseModel, Field

from orchestration.llm.client import LLMClient
from orchestration.stages.base import StageContext, StageCrew
from orchestration.stages.planner import _build_crewai_llm


class DocsUpdate(BaseModel):
    # Default to empty lists so `DocsUpdate()` is constructible and so a model
    # that omits one of the fields can still be validated.
    files_added: list[str] = Field(default_factory=list)
    files_updated: list[str] = Field(default_factory=list)

    # Tell pytest not to try to collect this BaseModel as a test class.
    __test__ = False


class DocsStage(StageCrew):
    name = "docs"
    output_schema = DocsUpdate

    # Tell pytest not to try to collect this stage class as a test class.
    __test__ = False

    def __init__(self) -> None:
        self.llm = _build_crewai_llm()
        self.agent = Agent(
            role="Docs",
            goal="Propose documentation updates (added and updated files) for a code change",
            backstory=(
                "You are a documentation specialist. You read a diff stat and decide "
                "which docs files need to be added or updated. You do not invent code — "
                "you only describe what already changed. You return paths; the stage "
                "commits whatever files appear at those paths."
            ),
            allow_delegation=False,
            llm=self.llm,
        )

    async def run(self, ctx: StageContext, llm: LLMClient) -> DocsUpdate:
        diff_stat = await self._get_diff_stat(ctx.worktree_path)
        repo_context = self._build_repo_context(ctx.worktree_path)

        task = Task(
            description=(
                f"Task: {ctx.task_title}\n{ctx.task_description or ''}\n\n"
                f"Worktree: {ctx.worktree_path}\n\n"
                f"Repo context:\n{repo_context}\n\n"
                f"Diff stat (`git diff main..HEAD --stat`):\n```\n"
                f"{diff_stat or '(empty)'}\n```\n\n"
                "Emit a DocsUpdate JSON: "
                "{files_added: string[], files_updated: string[]}.\n"
                "- `files_added` lists paths of NEW docs files you created "
                "(e.g., a new docs/foo.md, a new tutorial under docs/).\n"
                "- `files_updated` lists paths of EXISTING docs files you "
                "modified (e.g., README.md, docs/superpowers/specs/*.md).\n"
                "Only list files you actually wrote or modified. If no "
                "documentation update is needed, return empty lists."
            ),
            expected_output="DocsUpdate JSON",
            agent=self.agent,
            output_pydantic=DocsUpdate,
        )
        crew = Crew(agents=[self.agent], tasks=[task])
        # Crew.kickoff() is synchronous and cannot be called from a running
        # event loop; run() is async, so prefer kickoff_async when available.
        if hasattr(crew, "kickoff_async"):
            result = await crew.kickoff_async()
        else:
            result = crew.kickoff()

        raw_output = getattr(result, "pydantic", None)
        if raw_output is None:
            raise ValueError(
                "Docs stage: LLM did not return structured DocsUpdate output"
            )
        update = DocsUpdate.model_validate(raw_output)

        # Only commit when the LLM proposed at least one path. An empty
        # proposal short-circuits so the stage never produces a no-op commit.
        if update.files_added or update.files_updated:
            await self._commit(ctx.worktree_path, ctx.task_title, update)

        return update

    @staticmethod
    async def _get_diff_stat(worktree_path: Path) -> str:
        """Return `git diff main..HEAD --stat` for the worktree, or "" on failure.

        A missing `main` ref or a missing git binary degrades to an empty stat
        rather than failing the run, matching the other M3 stages.
        """
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["git", "diff", "main..HEAD", "--stat"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                errors="replace",
            )
        except (FileNotFoundError, NotADirectoryError):
            return ""

        if proc.returncode != 0:
            return ""
        return proc.stdout or ""

    @staticmethod
    def _build_repo_context(worktree_path: Path) -> str:
        """Return a small description of likely docs paths for the LLM prompt."""
        return (
            f"- README: {worktree_path / 'README.md'}\n"
            f"- docs/: {worktree_path / 'docs'}\n"
            "- src/orchestration/: package code (module-level docstrings).\n"
        )

    @staticmethod
    async def _commit(
        worktree_path: Path, task_title: str, update: DocsUpdate
    ) -> None:
        """Stage the LLM-proposed paths and commit. Ignores 'nothing to commit'.

        Ensures a git identity is configured before the commit so that a
        fresh worktree without a global identity does not fail.
        """
        await asyncio.to_thread(_ensure_git_identity, worktree_path)

        paths = list(update.files_added) + list(update.files_updated)
        # `git add -A` already covers every change in the worktree, but the
        # explicit paths make the stage's intent (and audit log) clearer and
        # avoid staging unrelated files if `-A` ever grew a narrower meaning.
        # Errors from non-existent LLM-proposed paths are tolerated; git's
        # `-A` keeps staging everything else, and `git commit` will simply
        # fail if nothing was actually staged.
        await asyncio.to_thread(
            subprocess.run,
            ["git", "add", "-A", *paths],
            cwd=str(worktree_path),
            capture_output=True,
        )

        await asyncio.to_thread(
            subprocess.run,
            ["git", "commit", "-m", f"docs: update for {task_title}"],
            cwd=str(worktree_path),
            capture_output=True,
        )


def _ensure_git_identity(worktree_path: Path) -> None:
    """Set a placeholder git identity if none is configured.

    Looks up `user.email` and `user.name` in the local repo config; if either
    is empty (or unset), sets both to the orchestration defaults. If a global
    identity is configured, those commands succeed and are a no-op.
    """
    email_proc = subprocess.run(
        ["git", "config", "user.email"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=False,
    )
    name_proc = subprocess.run(
        ["git", "config", "user.name"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if not email_proc.stdout.strip() or not name_proc.stdout.strip():
        subprocess.run(
            ["git", "config", "user.email", "orchestration@example.com"],
            cwd=str(worktree_path),
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Orchestration"],
            cwd=str(worktree_path),
            check=False,
            capture_output=True,
        )

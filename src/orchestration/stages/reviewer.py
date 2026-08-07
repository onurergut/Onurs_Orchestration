"""Reviewer stage: LLM reviews `git diff main..HEAD` and emits structured comments.

Read-only by construction: this stage runs a single `git diff` and sends the
result to the LLM. It never writes to the worktree — it only comments.

The report's `severity` and `approve_recommend` are always *recomputed* from the
returned comments rather than trusted from the model, so the approve gate stays
deterministic even when the LLM's own summary fields disagree with its comments.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from crewai import Agent, Crew, Task
from pydantic import BaseModel, field_validator

from orchestration.llm.client import LLMClient
from orchestration.stages.base import StageContext, StageCrew
from orchestration.stages.planner import _build_crewai_llm

# Severity ordering used to aggregate per-comment severities into the report's
# single `severity` field. Mirrors the ordering in security.py.
_SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# Synonyms an LLM may emit despite the prompt constraining it to low|medium|high.
# Anything still unrecognised falls back to "medium": mapping it to "low" would
# silently hide a real blocker, while "high" would let a single typo veto the run.
_SEVERITY_ALIASES: dict[str, str] = {
    "critical": "high",
    "blocker": "high",
    "blocking": "high",
    "severe": "high",
    "error": "high",
    "major": "medium",
    "moderate": "medium",
    "warning": "medium",
    "warn": "medium",
    "minor": "low",
    "trivial": "low",
    "nit": "low",
    "nitpick": "low",
    "info": "low",
    "informational": "low",
    "note": "low",
    "suggestion": "low",
    "style": "low",
}

# Diffs beyond this many characters are truncated before being sent to the LLM,
# to stay inside the context window on large changesets.
_MAX_DIFF_CHARS = 50_000

_DEFAULT_SEVERITY = "low"
_FALLBACK_SEVERITY = "medium"

_TRUNCATION_MARKER = (
    "\n\n[... diff truncated at {limit} characters; "
    "review the portion shown above ...]"
)


def _normalize_severity(raw: str) -> str:
    """Coerce a model-supplied severity into one of low|medium|high."""
    value = str(raw or "").strip().lower()
    if value in _SEVERITY_ORDER:
        return value
    if value in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[value]
    if not value:
        return _DEFAULT_SEVERITY
    return _FALLBACK_SEVERITY


class Comment(BaseModel):
    file: str
    line: int | None = None
    severity: str = _DEFAULT_SEVERITY
    body: str

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value: object) -> str:
        return _normalize_severity(value if isinstance(value, str) else "")


class ReviewReport(BaseModel):
    comments: list[Comment]
    severity: str
    approve_recommend: bool


def _aggregate_severity(comments: list[Comment]) -> str:
    """Return the maximum severity across comments (low < medium < high)."""
    if not comments:
        return _DEFAULT_SEVERITY
    return max(
        (c.severity for c in comments),
        key=lambda s: _SEVERITY_ORDER.get(s, _SEVERITY_ORDER[_FALLBACK_SEVERITY]),
    )


def _build_report(comments: list[Comment]) -> ReviewReport:
    """Build a ReviewReport with severity/approve_recommend derived from comments."""
    severity = _aggregate_severity(comments)
    return ReviewReport(
        comments=comments,
        severity=severity,
        approve_recommend=severity != "high",
    )


class ReviewerStage(StageCrew):
    name = "reviewer"
    output_schema = ReviewReport

    def __init__(self) -> None:
        self.llm = _build_crewai_llm()
        self.agent = Agent(
            role="Reviewer",
            goal="Review a git diff and emit structured, actionable review comments",
            backstory=(
                "You are a rigorous code reviewer. You read diffs closely and flag "
                "correctness bugs, security issues, and missing tests. You never "
                "modify files — you only comment."
            ),
            allow_delegation=False,
            llm=self.llm,
        )

    async def run(self, ctx: StageContext, llm: LLMClient) -> ReviewReport:
        diff = await self._get_diff(ctx.worktree_path)
        if not diff.strip():
            # Nothing changed (or the diff could not be computed) — there is
            # nothing to review, so approve rather than block the pipeline.
            return _build_report([])

        if len(diff) > _MAX_DIFF_CHARS:
            diff = diff[:_MAX_DIFF_CHARS] + _TRUNCATION_MARKER.format(
                limit=_MAX_DIFF_CHARS
            )

        task = Task(
            description=(
                f"Review the code change below.\n\n"
                f"Task: {ctx.task_title}\n{ctx.task_description or ''}\n\n"
                f"Worktree: {ctx.worktree_path}\n\n"
                f"Diff (`git diff main..HEAD`):\n```diff\n{diff}\n```\n\n"
                "Emit a ReviewReport JSON: "
                "{comments: [{file: string, line: integer|null, "
                "severity: \"low\"|\"medium\"|\"high\", body: string}], "
                "severity: \"low\"|\"medium\"|\"high\", approve_recommend: boolean}\n"
                "Each comment must reference a file from the diff, and `line` must "
                "be a line number in that file (or null for a file-level comment). "
                "Use severity \"high\" only for changes that must not merge as-is. "
                "Do not modify any files; only report comments."
            ),
            expected_output="ReviewReport JSON",
            agent=self.agent,
            output_pydantic=ReviewReport,
        )
        crew = Crew(agents=[self.agent], tasks=[task])
        # Crew.kickoff() is synchronous and cannot be called from a running event
        # loop; run() is async, so prefer kickoff_async when available.
        if hasattr(crew, "kickoff_async"):
            result = await crew.kickoff_async()
        else:
            result = crew.kickoff()

        raw_output = getattr(result, "pydantic", None)
        if raw_output is None:
            raise ValueError(
                "Reviewer stage: LLM did not return structured ReviewReport output"
            )
        report = ReviewReport.model_validate(raw_output)
        # Recompute the summary fields so the approve gate is deterministic.
        return _build_report(report.comments)

    @staticmethod
    async def _get_diff(worktree_path: Path) -> str:
        """Return `git diff main..HEAD` for the worktree, or "" if unavailable.

        A missing `main` ref or a missing git binary degrades to an empty diff
        rather than failing the run, matching the other M3 stages.
        """
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["git", "diff", "main..HEAD"],
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

"""Tests for ReviewerStage."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from orchestration.stages.base import StageContext
from orchestration.stages.reviewer import (
    Comment,
    ReviewerStage,
    ReviewReport,
    _aggregate_severity,
    _build_report,
    _normalize_severity,
)


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ReviewerStage.__init__ builds a CrewAI LLM from LLM_* env vars.

    Constructing the LLM/Agent does no network I/O, so dummy credentials are
    enough to exercise __init__ hermetically.
    """
    monkeypatch.setenv("LLM_MODEL", "dummy-model")
    monkeypatch.setenv("LLM_API_KEY", "dummy-key")
    monkeypatch.setenv("LLM_PROTOCOL", "anthropic")
    monkeypatch.delenv("LLM_ENDPOINT_URL", raising=False)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _git_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        check=False,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A git repo on branch `main` with a single committed file."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def _ctx(worktree: Path) -> StageContext:
    return StageContext(
        run_id=uuid4(),
        project_id=uuid4(),
        task_external_id="24",
        task_title="review smoke",
        task_description=None,
        worktree_path=worktree,
        previous_artifacts={},
    )


# --- metadata / protocol ----------------------------------------------------


def test_reviewer_stage_metadata(llm_env: None):
    stage = ReviewerStage()
    assert stage.name == "reviewer"
    assert stage.output_schema is ReviewReport
    assert stage.output_schema.__name__ == "ReviewReport"


def test_reviewer_implements_stage_crew_protocol(llm_env: None):
    stage = ReviewerStage()
    for attr in ("name", "output_schema", "run"):
        assert hasattr(stage, attr)
    assert callable(stage.run)
    assert stage.agent.role == "Reviewer"


# --- severity normalization -------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("HIGH", "high"),
        ("  High  ", "high"),
        ("critical", "high"),
        ("blocker", "high"),
        ("warning", "medium"),
        ("nit", "low"),
        ("info", "low"),
        ("", "low"),
    ],
)
def test_normalize_severity(raw: str, expected: str):
    assert _normalize_severity(raw) == expected


def test_normalize_severity_unknown_falls_back_to_medium():
    """Unknown values must neither hide a blocker nor auto-veto the run."""
    assert _normalize_severity("spicy") == "medium"


def test_comment_normalizes_severity_on_construction():
    assert Comment(file="a.py", line=1, severity="CRITICAL", body="b").severity == "high"


# --- aggregation ------------------------------------------------------------


def test_aggregate_severity_empty_is_low():
    assert _aggregate_severity([]) == "low"


def test_aggregate_severity_takes_max():
    comments = [
        Comment(file="a.py", line=1, severity="low", body="x"),
        Comment(file="b.py", line=2, severity="high", body="y"),
        Comment(file="c.py", line=3, severity="medium", body="z"),
    ]
    assert _aggregate_severity(comments) == "high"


def test_aggregate_severity_medium_when_no_high():
    comments = [
        Comment(file="a.py", line=1, severity="low", body="x"),
        Comment(file="b.py", line=None, severity="medium", body="y"),
    ]
    assert _aggregate_severity(comments) == "medium"


def test_build_report_approves_when_not_high():
    report = _build_report([Comment(file="a.py", line=1, severity="medium", body="x")])
    assert report.severity == "medium"
    assert report.approve_recommend is True


def test_build_report_blocks_on_high():
    report = _build_report([Comment(file="a.py", line=1, severity="high", body="x")])
    assert report.severity == "high"
    assert report.approve_recommend is False


def test_build_report_empty_approves():
    report = _build_report([])
    assert report.comments == []
    assert report.severity == "low"
    assert report.approve_recommend is True


# --- diff retrieval (real git) ---------------------------------------------


async def test_get_diff_empty_when_no_changes(git_repo: Path):
    assert await ReviewerStage._get_diff(git_repo) == ""


async def test_get_diff_returns_changes_on_branch(git_repo: Path):
    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "change")

    diff = await ReviewerStage._get_diff(git_repo)
    assert "app.py" in diff
    assert "return a - b" in diff


async def test_get_diff_degrades_when_main_missing(tmp_path: Path):
    """A repo with no `main` ref must not raise — it degrades to an empty diff."""
    _git(tmp_path, "init", "-b", "other")
    assert await ReviewerStage._get_diff(tmp_path) == ""


async def test_get_diff_degrades_outside_repo(tmp_path: Path):
    assert await ReviewerStage._get_diff(tmp_path) == ""


# --- run() short-circuit (no LLM call) -------------------------------------


async def test_run_returns_approved_report_when_diff_empty(
    llm_env: None, git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """An empty diff must short-circuit before any Crew/LLM work happens."""
    import orchestration.stages.reviewer as reviewer_mod

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("Crew must not be constructed for an empty diff")

    monkeypatch.setattr(reviewer_mod, "Crew", _boom)

    report = await ReviewerStage().run(_ctx(git_repo), llm=None)  # type: ignore[arg-type]

    assert isinstance(report, ReviewReport)
    assert report.comments == []
    assert report.severity == "low"
    assert report.approve_recommend is True


async def test_run_is_read_only(
    llm_env: None, git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """Reviewer must not modify the worktree (spec: comments only)."""
    import orchestration.stages.reviewer as reviewer_mod

    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "change")

    before = _git_status(git_repo)

    class _FakeResult:
        pydantic = ReviewReport(
            comments=[
                Comment(file="app.py", line=2, severity="high", body="subtraction bug")
            ],
            # Deliberately inconsistent with the comments below, to prove the
            # stage recomputes rather than trusting the model.
            severity="low",
            approve_recommend=True,
        )

    class _FakeCrew:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def kickoff_async(self) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(reviewer_mod, "Crew", _FakeCrew)

    report = await ReviewerStage().run(_ctx(git_repo), llm=None)  # type: ignore[arg-type]

    after = _git_status(git_repo)
    assert before == after, "reviewer stage modified the worktree"

    # Summary fields are recomputed from the comments, overriding the model.
    assert report.severity == "high"
    assert report.approve_recommend is False


async def test_run_truncates_large_diff(
    llm_env: None, git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """Diffs over the cap are truncated before reaching the LLM."""
    import orchestration.stages.reviewer as reviewer_mod

    huge = "x" * (reviewer_mod._MAX_DIFF_CHARS + 10_000)
    monkeypatch.setattr(
        ReviewerStage, "_get_diff", staticmethod(lambda _p: _async_value(huge))
    )

    captured: dict[str, str] = {}

    class _FakeTask:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["description"] = str(kwargs.get("description", ""))

    class _FakeResult:
        pydantic = ReviewReport(comments=[], severity="low", approve_recommend=True)

    class _FakeCrew:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def kickoff_async(self) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(reviewer_mod, "Task", _FakeTask)
    monkeypatch.setattr(reviewer_mod, "Crew", _FakeCrew)

    await ReviewerStage().run(_ctx(git_repo), llm=None)  # type: ignore[arg-type]

    description = captured["description"]
    assert "diff truncated" in description
    # Assert on the longest consecutive run rather than a total count: the
    # worktree path is interpolated into the prompt and may itself contain "x".
    assert "x" * reviewer_mod._MAX_DIFF_CHARS in description
    assert "x" * (reviewer_mod._MAX_DIFF_CHARS + 1) not in description


async def _async_value(value: str) -> str:
    return value


async def test_run_raises_when_llm_returns_no_structured_output(
    llm_env: None, git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """A parse failure must surface, not silently approve."""
    import orchestration.stages.reviewer as reviewer_mod

    monkeypatch.setattr(
        ReviewerStage, "_get_diff", staticmethod(lambda _p: _async_value("some diff"))
    )

    class _FakeResult:
        pydantic = None

    class _FakeCrew:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def kickoff_async(self) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(reviewer_mod, "Crew", _FakeCrew)

    with pytest.raises(ValueError, match="structured ReviewReport"):
        await ReviewerStage().run(_ctx(git_repo), llm=None)  # type: ignore[arg-type]

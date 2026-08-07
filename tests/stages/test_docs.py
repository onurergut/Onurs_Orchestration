"""Tests for DocsStage."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from orchestration.stages.base import StageContext
from orchestration.stages.docs import DocsStage, DocsUpdate


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DocsStage.__init__ builds a CrewAI LLM from LLM_* env vars.

    Constructing the LLM/Agent does no network I/O, so dummy credentials are
    enough to exercise __init__ hermetically.
    """
    monkeypatch.setenv("LLM_MODEL", "dummy-model")
    monkeypatch.setenv("LLM_API_KEY", "dummy-key")
    monkeypatch.setenv("LLM_PROTOCOL", "anthropic")
    monkeypatch.delenv("LLM_ENDPOINT_URL", raising=False)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _git_log(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "--oneline"],
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
        task_external_id="25",
        task_title="docs smoke",
        task_description=None,
        worktree_path=worktree,
        previous_artifacts={},
    )


# --- metadata / protocol ----------------------------------------------------


def test_docs_stage_metadata(llm_env: None):
    stage = DocsStage()
    assert stage.name == "docs"
    assert stage.output_schema is DocsUpdate
    assert stage.output_schema.__name__ == "DocsUpdate"


def test_docs_implements_stage_crew_protocol(llm_env: None):
    stage = DocsStage()
    for attr in ("name", "output_schema", "run"):
        assert hasattr(stage, attr)
    assert callable(stage.run)
    assert stage.agent.role == "Docs"


# --- schema -----------------------------------------------------------------


def test_docs_update_defaults_to_empty_lists():
    """A freshly-constructed DocsUpdate has both lists empty, not None."""
    update = DocsUpdate()
    assert update.files_added == []
    assert update.files_updated == []


# --- repo context prompt ----------------------------------------------------


def test_build_repo_context_mentions_readme_and_docs(tmp_path: Path):
    text = DocsStage._build_repo_context(tmp_path)
    assert "README.md" in text
    assert "docs/" in text
    assert "src/orchestration/" in text


# --- diff stat retrieval (real git) -----------------------------------------


async def test_get_diff_stat_empty_when_no_changes(git_repo: Path):
    assert await DocsStage._get_diff_stat(git_repo) == ""


async def test_get_diff_stat_returns_changes_on_branch(git_repo: Path):
    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "change")

    stat = await DocsStage._get_diff_stat(git_repo)
    assert "app.py" in stat
    assert "1 file changed" in stat


async def test_get_diff_stat_degrades_when_main_missing(tmp_path: Path):
    """A repo with no `main` ref must not raise — it degrades to empty."""
    _git(tmp_path, "init", "-b", "other")
    assert await DocsStage._get_diff_stat(tmp_path) == ""


async def test_get_diff_stat_degrades_outside_repo(tmp_path: Path):
    assert await DocsStage._get_diff_stat(tmp_path) == ""


# --- commit -----------------------------------------------------------------


async def test_commit_stages_and_commits_added_file(
    llm_env: None, git_repo: Path
):
    """An added file proposed by the LLM is staged and committed."""
    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "docs").mkdir()
    (git_repo / "docs/foo.md").write_text("# Foo\n")
    # Leave the file untracked — the stage's `git add -A` must pick it up.

    before_log = _git_log(git_repo)
    await DocsStage._commit(
        git_repo,
        "smoke task",
        DocsUpdate(files_added=["docs/foo.md"], files_updated=[]),
    )
    after_log = _git_log(git_repo)

    assert before_log != after_log, "commit did not happen"
    assert "docs: update for smoke task" in after_log


async def test_commit_stages_and_commits_updated_file(
    llm_env: None, git_repo: Path
):
    """An updated file proposed by the LLM is staged and committed."""
    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "README.md").write_text("# Orchestration System\n\nUpdated docs.\n")

    before_log = _git_log(git_repo)
    await DocsStage._commit(
        git_repo,
        "smoke task",
        DocsUpdate(files_added=[], files_updated=["README.md"]),
    )
    after_log = _git_log(git_repo)

    assert before_log != after_log, "commit did not happen"
    assert "docs: update for smoke task" in after_log


async def test_commit_tolerates_nothing_to_commit(
    llm_env: None, git_repo: Path
):
    """A proposed path that does not exist must not crash the stage."""
    _git(git_repo, "checkout", "-b", "feature")
    # No actual file changes; commit will report nothing to commit.

    before_log = _git_log(git_repo)
    # Must not raise.
    await DocsStage._commit(
        git_repo,
        "smoke task",
        DocsUpdate(files_added=["docs/never-created.md"], files_updated=[]),
    )
    after_log = _git_log(git_repo)
    # No commit was made (nothing was actually staged) — log unchanged.
    assert before_log == after_log


# --- run() short-circuit (no LLM call) --------------------------------------


async def test_run_short_circuits_when_update_empty(
    llm_env: None, git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """An empty DocsUpdate must short-circuit before any commit work."""
    import orchestration.stages.docs as docs_mod

    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "app.py").write_text("changed\n")

    before_log = _git_log(git_repo)

    def _boom_commit(*args: object, **kwargs: object) -> None:
        raise AssertionError("_commit must not run for empty DocsUpdate")

    monkeypatch.setattr(docs_mod.DocsStage, "_commit", _boom_commit)

    class _FakeResult:
        pydantic = DocsUpdate(files_added=[], files_updated=[])

    class _FakeCrew:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def kickoff_async(self) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(docs_mod, "Crew", _FakeCrew)

    update = await DocsStage().run(_ctx(git_repo), llm=None)  # type: ignore[arg-type]

    assert isinstance(update, DocsUpdate)
    assert update.files_added == []
    assert update.files_updated == []
    after_log = _git_log(git_repo)
    assert before_log == after_log, "empty DocsUpdate should not commit"


async def test_run_returns_docs_update_and_commits(
    llm_env: None, git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """A non-empty DocsUpdate is committed before run() returns."""
    import orchestration.stages.docs as docs_mod

    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "docs").mkdir()
    (git_repo / "docs/new.md").write_text("# New\n")

    before_log = _git_log(git_repo)

    class _FakeResult:
        pydantic = DocsUpdate(
            files_added=["docs/new.md"], files_updated=[]
        )

    class _FakeCrew:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def kickoff_async(self) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(docs_mod, "Crew", _FakeCrew)

    update = await DocsStage().run(_ctx(git_repo), llm=None)  # type: ignore[arg-type]

    assert isinstance(update, DocsUpdate)
    assert update.files_added == ["docs/new.md"]
    after_log = _git_log(git_repo)
    assert before_log != after_log
    assert "docs: update for docs smoke" in after_log


async def test_run_passes_diff_stat_and_context_to_llm(
    llm_env: None, git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """The CrewAI Task description must include diff stat + repo context."""
    import orchestration.stages.docs as docs_mod

    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "change")

    captured: dict[str, str] = {}

    class _FakeTask:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["description"] = str(kwargs.get("description", ""))

    class _FakeResult:
        pydantic = DocsUpdate(files_added=[], files_updated=[])

    class _FakeCrew:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def kickoff_async(self) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(docs_mod, "Task", _FakeTask)
    monkeypatch.setattr(docs_mod, "Crew", _FakeCrew)

    await DocsStage().run(_ctx(git_repo), llm=None)  # type: ignore[arg-type]

    description = captured["description"]
    assert "app.py" in description, "diff stat missing from prompt"
    assert "README.md" in description, "repo context missing"
    assert "docs/" in description, "docs/ context missing"
    assert "docs smoke" in description, "task title missing"


async def test_run_raises_when_llm_returns_no_structured_output(
    llm_env: None, git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """A parse failure must surface, not silently produce empty docs."""
    import orchestration.stages.docs as docs_mod

    _git(git_repo, "checkout", "-b", "feature")
    (git_repo / "app.py").write_text("changed\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "change")

    class _FakeResult:
        pydantic = None

    class _FakeCrew:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def kickoff_async(self) -> _FakeResult:
            return _FakeResult()

    monkeypatch.setattr(docs_mod, "Crew", _FakeCrew)

    with pytest.raises(ValueError, match="structured DocsUpdate"):
        await DocsStage().run(_ctx(git_repo), llm=None)  # type: ignore[arg-type]

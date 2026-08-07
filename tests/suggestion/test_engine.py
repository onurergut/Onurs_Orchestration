"""Tests for ``orchestration.suggestion.engine.SuggestionEngine``."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from orchestration.adapters.task_source.base import (
    Comment,
    CreateTask,
    ExternalTask,
    TaskSourceAdapter,
)
from orchestration.git.worktree import WorktreeManager
from orchestration.suggestion.engine import (
    Proposal,
    SuggestionEngine,
    _ProposalList,
)

# ---------- Test doubles ----------


class _FakeTaskSource(TaskSourceAdapter):
    """In-memory TaskSourceAdapter for tests."""

    name = "fake"

    def __init__(self, tasks: list[ExternalTask] | None = None) -> None:
        self.tasks = tasks or []
        self.list_calls: list[tuple[Any, str | None]] = []

    async def list_tasks(self, project_id, status=None):
        self.list_calls.append((project_id, status))
        return list(self.tasks)

    async def get_task(self, task_id):
        for t in self.tasks:
            if t.external_id == task_id:
                return t
        raise KeyError(task_id)

    async def create_task(self, project_id, payload: CreateTask):
        return ExternalTask(
            external_id="new", title=payload.title, description=payload.description
        )

    async def update_status(self, task_id, status):
        return None

    async def add_comment(self, task_id, body):
        return Comment(external_id=task_id, body=body)


class _FakeLLM:
    """Stand-in for the LLMClient Protocol; we don't actually call it."""

    endpoint_name = "fake"

    async def complete(self, request):  # pragma: no cover - never invoked
        raise NotImplementedError


class _FakeCrewResult:
    def __init__(self, pydantic):
        self.pydantic = pydantic


class _FakeCrew:
    """Bypass CrewAI by returning a pre-built _ProposalList from kickoff."""

    def __init__(self, *args, **kwargs):
        self.last_proposals: list[Proposal] = []

    async def kickoff_async(self):
        return _FakeCrewResult(_ProposalList(proposals=self.last_proposals))

    def kickoff(self):
        return _FakeCrewResult(_ProposalList(proposals=self.last_proposals))


# ---------- Helpers ----------


def _git_init(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


def _make_engine(
    tmp_path: Path,
    tasks: list[ExternalTask] | None = None,
) -> SuggestionEngine:
    return SuggestionEngine(
        llm=_FakeLLM(),  # type: ignore[arg-type]
        worktree_manager=WorktreeManager(tmp_path / "wt-base"),
        task_source=_FakeTaskSource(tasks),
    )


# ---------- Proposal schema ----------


def test_proposal_schema_accepts_valid_payload():
    p = Proposal(
        title="Add caching layer",
        description="Add Redis caching to /lookup",
        rationale="Reduces DB load under bursty traffic",
        estimated_complexity="medium",
        related_files=["src/orchestration/cache.py"],
    )
    assert p.title == "Add caching layer"
    assert p.estimated_complexity == "medium"
    assert p.related_files == ["src/orchestration/cache.py"]


def test_proposal_rejects_invalid_complexity():
    with pytest.raises(ValidationError):
        Proposal(
            title="x",
            description="y",
            rationale="z",
            estimated_complexity="huge",  # not in the literal set
        )


def test_proposal_related_files_default_to_empty():
    p = Proposal(title="x", description="y", rationale="z", estimated_complexity="small")
    assert p.related_files == []


# ---------- Instantiation ----------


def test_engine_instantiates_with_mock_deps(tmp_path):
    eng = _make_engine(tmp_path)
    assert hasattr(eng, "suggest")
    assert callable(eng.suggest)
    assert eng.llm.endpoint_name == "fake"
    assert isinstance(eng.worktree_manager, WorktreeManager)
    assert eng.task_source.name == "fake"


# ---------- Context reading ----------


def test_read_repo_context_includes_readme_and_top_level(tmp_path):
    repo = tmp_path / "r"
    _git_init(repo, {
        "README.md": "# Hello World\n\nThis is the readme.",
        "main.py": "print('hi')",
        "pkg/__init__.py": "",
    })
    eng = _make_engine(tmp_path)
    ctx = eng._read_repo_context(repo)
    assert "README.md" in ctx
    assert "Hello World" in ctx
    assert "main.py" in ctx
    assert "Top-level entries" in ctx


def test_read_repo_context_handles_missing_readme(tmp_path):
    repo = tmp_path / "r"
    _git_init(repo, {"main.py": "x = 1"})
    eng = _make_engine(tmp_path)
    ctx = eng._read_repo_context(repo)
    assert "no README.md present" in ctx


def test_recent_files_returns_repo_relative_paths_in_order(tmp_path):
    repo = tmp_path / "r"
    _git_init(
        repo,
        {"first.py": "a", "second.py": "b", "third.py": "c"},
    )
    eng = _make_engine(tmp_path)
    files = eng._recent_files(repo, limit=5)
    assert files  # at least one
    assert all(isinstance(f, str) for f in files)
    assert len(files) <= 5
    # All paths are repo-relative
    assert all(not f.startswith("/") for f in files)


# ---------- Clone ----------


def test_git_clone_copies_local_repo(tmp_path):
    """_git_clone (the static helper) handles both URL and local-path inputs."""
    import tempfile

    source = tmp_path / "source"
    _git_init(source, {"README.md": "x", "a.py": "y"})

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "cloned"
        SuggestionEngine._git_clone(str(source), target)
        assert target.exists()
        assert (target / "README.md").read_text() == "x"
        assert (target / "a.py").read_text() == "y"
        # git history is present in the clone (we asked for --depth 50).
        r = subprocess.run(
            ["git", "log", "--oneline"], cwd=target, capture_output=True, text=True, check=True
        )
        assert "init" in r.stdout


# ---------- End-to-end with mocked LLM ----------


@pytest.fixture
def _llm_env(monkeypatch):
    """Stub the LLM env vars so _build_crewai_llm() can construct a CrewAI LLM."""
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROTOCOL", "openai")


@pytest.fixture
def _fake_crew(monkeypatch):
    """Replace CrewAI's Crew with a fake that returns canned proposals."""
    fake = _FakeCrew()
    monkeypatch.setattr("orchestration.suggestion.engine.Crew", lambda *a, **kw: fake)
    return fake


@pytest.mark.asyncio
async def test_suggest_returns_proposals_and_persists_row(
    tmp_path, _llm_env, _fake_crew
):
    from orchestration.storage.db import session_scope
    from orchestration.storage.models.project import Project
    from orchestration.storage.models.suggestion import Suggestion

    project_id = uuid.uuid4()
    source = tmp_path / "source"
    _git_init(source, {"README.md": "# Demo", "app.py": "x = 1"})

    async with session_scope() as session:
        session.add(Project(
            id=project_id,
            name="demo",
            repo_url=str(source),
            default_branch="main",
        ))

    expected = [
        Proposal(
            title="Add health-check endpoint",
            description="Expose /readyz with DB ping",
            rationale="Improves operational visibility",
            estimated_complexity="small",
            related_files=["src/orchestration/api/app.py"],
        ),
        Proposal(
            title="Migrate audit log to JSONB",
            description="Switch audit metadata column to JSONB",
            rationale="Easier to evolve audit shape",
            estimated_complexity="medium",
            related_files=["alembic/versions/"],
        ),
    ]
    _fake_crew.last_proposals = expected

    tasks = [
        ExternalTask(external_id="T1", title="Add CI"),
        ExternalTask(external_id="T2", title="Document API"),
    ]
    eng = SuggestionEngine(
        llm=_FakeLLM(),  # type: ignore[arg-type]
        worktree_manager=WorktreeManager(tmp_path / "wt-base"),
        task_source=_FakeTaskSource(tasks),
    )

    result = await eng.suggest(project_id)
    assert result == expected
    assert all(isinstance(p, Proposal) for p in result)

    # Suggestion row was persisted with status='pending' and the proposals JSONB.
    async with session_scope() as session:
        rows = (await session.execute(
            select(Suggestion).where(Suggestion.project_id == project_id)
        )).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "pending"
    assert row.proposals == [p.model_dump() for p in expected]
    # The task source was queried with status='pending'.
    assert eng.task_source.list_calls == [(project_id, "pending")]


@pytest.mark.asyncio
async def test_suggest_raises_for_missing_project(tmp_path, _llm_env, _fake_crew):
    eng = SuggestionEngine(
        llm=_FakeLLM(),  # type: ignore[arg-type]
        worktree_manager=WorktreeManager(tmp_path / "wt-base"),
        task_source=_FakeTaskSource(),
    )
    with pytest.raises(ValueError, match="not found"):
        await eng.suggest(uuid.uuid4())


@pytest.mark.asyncio
async def test_suggest_empty_proposals_still_persists_row(
    tmp_path, _llm_env, _fake_crew
):
    from orchestration.storage.db import session_scope
    from orchestration.storage.models.project import Project
    from orchestration.storage.models.suggestion import Suggestion

    project_id = uuid.uuid4()
    source = tmp_path / "source"
    _git_init(source, {"README.md": "x"})

    async with session_scope() as session:
        session.add(Project(
            id=project_id,
            name="demo",
            repo_url=str(source),
            default_branch="main",
        ))

    _fake_crew.last_proposals = []  # LLM returned nothing useful

    eng = SuggestionEngine(
        llm=_FakeLLM(),  # type: ignore[arg-type]
        worktree_manager=WorktreeManager(tmp_path / "wt-base"),
        task_source=_FakeTaskSource(),
    )
    result = await eng.suggest(project_id)
    assert result == []

    async with session_scope() as session:
        rows = (await session.execute(
            select(Suggestion).where(Suggestion.project_id == project_id)
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].proposals == []
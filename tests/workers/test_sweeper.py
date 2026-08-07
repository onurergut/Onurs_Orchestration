import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from orchestration.config import settings
from orchestration.git.worktree import WorktreeManager
from orchestration.storage.db import session_scope
from orchestration.storage.models.project import Project
from orchestration.storage.models.worktree import Worktree
from orchestration.workers.sweeper import _is_local_repo_url, sweep_once


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


async def _fetch_worktree(wt_id: uuid.UUID) -> Worktree | None:
    async with session_scope() as session:
        return (
            await session.execute(select(Worktree).where(Worktree.id == wt_id))
        ).scalar_one_or_none()


def test_is_local_repo_url(tmp_path: Path) -> None:
    assert _is_local_repo_url(str(tmp_path)) is True
    assert _is_local_repo_url("https://github.com/foo/bar.git") is False
    assert _is_local_repo_url("git@github.com:foo/bar.git") is False
    assert _is_local_repo_url("ssh://git@example.com/repo.git") is False
    assert _is_local_repo_url("git://example.com/repo.git") is False
    assert _is_local_repo_url("") is False
    assert _is_local_repo_url(None) is False
    # Path that doesn't exist on disk is not considered local.
    assert _is_local_repo_url("/nonexistent/path/xyz") is False


@pytest.mark.asyncio
async def test_sweep_once_empty_db_returns_zero(git_repo: Path, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worktree_base_path", str(tmp_path / "wts"))
    # No projects, no worktrees — nothing to do.
    assert await sweep_once() == 0


@pytest.mark.asyncio
async def test_sweep_once_cleans_only_expired_archived(
    git_repo: Path, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "worktree_base_path", str(tmp_path / "wts"))
    wm = WorktreeManager(git_repo)

    # Three real worktrees on disk so cleanup has something to remove.
    past_wt = await wm.create(project_id=None, task_id="past")
    future_wt = await wm.create(project_id=None, task_id="future")
    active_wt = await wm.create(project_id=None, task_id="active")

    project_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_scope() as session:
        session.add(Project(id=project_id, name="t", repo_url=str(git_repo)))
        await session.flush()
        session.add(
            Worktree(
                id=past_wt.id,
                project_id=project_id,
                path=str(past_wt.path),
                branch=past_wt.branch,
                base_sha=past_wt.base_sha,
                status="archived",
                expires_at=now - timedelta(days=1),
            )
        )
        session.add(
            Worktree(
                id=future_wt.id,
                project_id=project_id,
                path=str(future_wt.path),
                branch=future_wt.branch,
                base_sha=future_wt.base_sha,
                status="archived",
                expires_at=now + timedelta(days=10),
            )
        )
        session.add(
            Worktree(
                id=active_wt.id,
                project_id=project_id,
                path=str(active_wt.path),
                branch=active_wt.branch,
                base_sha=active_wt.base_sha,
                status="active",
                expires_at=now - timedelta(days=1),
            )
        )

    # Sanity: all three worktrees exist on disk before the sweep.
    assert past_wt.path.exists()
    assert future_wt.path.exists()
    assert active_wt.path.exists()

    swept = await sweep_once()
    assert swept == 1

    # On disk: only the expired+archived one is gone.
    assert not past_wt.path.exists()
    assert future_wt.path.exists()
    assert active_wt.path.exists()

    # In DB: status reflects the transition; only ``past`` is ``deleted``.
    past = await _fetch_worktree(past_wt.id)
    future = await _fetch_worktree(future_wt.id)
    active = await _fetch_worktree(active_wt.id)
    assert past is not None
    assert future is not None
    assert active is not None
    assert past.status == "deleted"
    assert past.cleaned_at is not None
    assert future.status == "archived"
    assert future.cleaned_at is None
    assert active.status == "active"
    assert active.cleaned_at is None


@pytest.mark.asyncio
async def test_sweep_once_skips_remote_only_project(
    git_repo: Path, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "worktree_base_path", str(tmp_path / "wts"))
    wm = WorktreeManager(git_repo)
    wt = await wm.create(project_id=None, task_id="remote-proj")

    project_id = uuid.uuid4()
    now = datetime.now(UTC)

    async with session_scope() as session:
        # Repo points to a remote URL — sweeper can't touch the filesystem.
        session.add(Project(id=project_id, name="remote", repo_url="https://github.com/foo/bar.git"))
        await session.flush()
        session.add(
            Worktree(
                id=wt.id,
                project_id=project_id,
                path=str(wt.path),
                branch=wt.branch,
                base_sha=wt.base_sha,
                status="archived",
                expires_at=now - timedelta(days=1),
            )
        )

    swept = await sweep_once()
    assert swept == 0

    row = await _fetch_worktree(wt.id)
    assert row is not None
    # Row left in 'archived' state — sweeper did not advance it.
    assert row.status == "archived"
    assert row.cleaned_at is None

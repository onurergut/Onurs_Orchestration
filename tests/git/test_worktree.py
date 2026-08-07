import subprocess

import pytest

from orchestration.git.worktree import WorktreeManager


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.asyncio
async def test_worktree_create_and_commit(git_repo, tmp_path, monkeypatch):
    monkeypatch.setattr("orchestration.config.settings.worktree_base_path", str(tmp_path / "wts"))
    mgr = WorktreeManager(git_repo)
    wt = await mgr.create(project_id=None, task_id="T1")
    assert wt.path.exists()
    (wt.path / "new.py").write_text("x = 1\n")
    sha = await mgr.commit(wt.id, "add x")
    assert sha
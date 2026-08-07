import asyncio
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from orchestration.config import settings


@dataclass
class Worktree:
    id: uuid.UUID
    path: Path
    branch: str
    base_sha: str


class WorktreeManager:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    async def create(self, project_id: uuid.UUID, task_id: str) -> Worktree:
        wt_id = uuid.uuid4()
        branch = f"orch/{task_id}"
        path = Path(settings.worktree_base_path) / str(wt_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        base_sha = await asyncio.to_thread(self._git, "rev-parse", "HEAD")
        await asyncio.to_thread(
            self._git, "worktree", "add", "-b", branch, str(path), "main"
        )
        return Worktree(id=wt_id, path=path, branch=branch, base_sha=base_sha)

    async def diff(self, worktree_id: uuid.UUID) -> str:
        path = Path(settings.worktree_base_path) / str(worktree_id)
        return await asyncio.to_thread(self._git_in, path, "diff", "main")

    async def commit(self, worktree_id: uuid.UUID, message: str) -> str:
        path = Path(settings.worktree_base_path) / str(worktree_id)
        await asyncio.to_thread(self._git_in, path, "add", "-A")
        await asyncio.to_thread(self._git_in, path, "commit", "-m", message)
        return await asyncio.to_thread(self._git_in, path, "rev-parse", "HEAD")

    async def push_branch(self, worktree_id: uuid.UUID) -> str:
        path = Path(settings.worktree_base_path) / str(worktree_id)
        wt = await self._find_by_path(path)
        await asyncio.to_thread(self._git_in, path, "push", "origin", wt.branch)
        return wt.branch

    async def cleanup(self, worktree_id: uuid.UUID) -> None:
        path = Path(settings.worktree_base_path) / str(worktree_id)
        if path.exists():
            await asyncio.to_thread(self._git, "worktree", "remove", "--force", str(path))
            shutil.rmtree(path, ignore_errors=True)

    async def _find_by_path(self, path: Path) -> Worktree:
        wt_id = uuid.UUID(path.name)
        branch = await asyncio.to_thread(self._git_in, path, "rev-parse", "--abbrev-ref", "HEAD")
        return Worktree(id=wt_id, path=path, branch=branch, base_sha="")

    def _git(self, *args: str) -> str:
        import subprocess
        r = subprocess.run(["git", *args], cwd=self.repo_path, capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def _git_in(self, path: Path, *args: str) -> str:
        import subprocess
        r = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True)
        return r.stdout.strip()
"""Background sweeper that deletes worktrees whose TTL has expired.

A worktree transitions through ``active`` -> ``archived`` -> ``deleted``.
Archive is set by the orchestrator when a run finishes; ``expires_at`` is
populated at archive time as ``now() + worktree_ttl_days``. This sweeper
periodically finds archived worktrees whose TTL has elapsed, deletes
the on-disk directory via :class:`WorktreeManager.cleanup`, and marks
the row as ``deleted`` with ``cleaned_at=now()``.

Remote-only projects (whose ``repo_url`` is not a local path we can
reach) are skipped — the sweeper cannot delete files on a remote host.
Individual cleanup failures are logged and do not abort the sweep.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update

from orchestration.git.worktree import WorktreeManager
from orchestration.storage.db import session_scope
from orchestration.storage.models.project import Project
from orchestration.storage.models.worktree import Worktree

_REMOTE_PREFIXES = ("http://", "https://", "git@", "ssh://", "git://")


def _is_local_repo_url(repo_url: str | None) -> bool:
    """Return True iff ``repo_url`` points to a directory we can touch locally."""
    if not repo_url:
        return False
    if repo_url.startswith(_REMOTE_PREFIXES):
        return False
    return Path(repo_url).is_dir()


async def sweep_once() -> int:
    """Sweep all expired archived worktrees once.

    Returns the count of worktrees successfully transitioned to
    ``deleted``. Failures on individual rows are caught and logged so
    a single bad worktree does not stop the rest of the sweep.
    """
    now = datetime.now(UTC)
    swept = 0

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Worktree).where(
                    Worktree.status == "archived",
                    Worktree.expires_at < now,
                )
            )
        ).scalars().all()

        # One WorktreeManager per project — avoid re-instantiating for
        # every worktree of the same repo.
        managers: dict[uuid.UUID, WorktreeManager | None] = {}

        for w in rows:
            try:
                if w.project_id not in managers:
                    project = (
                        await session.execute(
                            select(Project).where(Project.id == w.project_id)
                        )
                    ).scalar_one_or_none()
                    if project is None or not _is_local_repo_url(project.repo_url):
                        managers[w.project_id] = None
                    else:
                        managers[w.project_id] = WorktreeManager(Path(project.repo_url))

                mgr = managers[w.project_id]
                if mgr is None:
                    print(
                        f"sweeper: skip {w.id} (project {w.project_id} "
                        "has no local repo_url)",
                        file=sys.stderr,
                    )
                    continue

                await mgr.cleanup(w.id)
                await session.execute(
                    update(Worktree)
                    .where(Worktree.id == w.id)
                    .values(status="deleted", cleaned_at=datetime.now(UTC))
                )
                swept += 1
            except Exception as exc:  # noqa: BLE001 — keep sweep alive on row errors
                print(
                    f"sweeper: failed to clean worktree {w.id}: {exc}",
                    file=sys.stderr,
                )
                continue

    return swept


async def run_sweeper_forever(interval_s: int = 300) -> None:
    """Run :func:`sweep_once` every ``interval_s`` seconds forever."""
    while True:
        try:
            swept = await sweep_once()
            print(f"sweeper: swept {swept} worktree(s)")
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            print(f"sweeper: sweep failed: {exc}", file=sys.stderr)
        await asyncio.sleep(interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Worktree TTL sweeper")
    parser.add_argument(
        "--forever",
        action="store_true",
        help="Loop forever; otherwise run one sweep and exit (cron-friendly).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds between sweeps when --forever is set (default: 300).",
    )
    args = parser.parse_args()

    if args.forever:
        asyncio.run(run_sweeper_forever(args.interval))
    else:
        swept = asyncio.run(sweep_once())
        print(f"sweeper: swept {swept} worktree(s)")


if __name__ == "__main__":
    main()

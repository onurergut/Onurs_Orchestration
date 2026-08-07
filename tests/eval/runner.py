"""SWE-bench Verified eval runner (M5/Task 36).

Iterates over ``tests/eval/swebench_verified_subset.jsonl`` and, for each
issue, drives the orchestration pipeline (Planner + Coder) against a
freshly-cloned worktree of the target repo at ``base_commit``. After the
pipeline finishes, applies the gold ``test_patch`` and runs pytest — exit
0 means the pipeline's edit made the failing tests pass.

This is a **skeleton** driver. It does the right structural work:

* skip on empty ``test_patch`` (current seed has ``test_patch == ""``
  for every row — see ``swebench_verified_subset.jsonl`` and Task 35
  report);
* clone → checkout → run pipeline → verify, all in one async function;
* aggregate PASS / FAIL / SKIP and write a timestamped JSON report.

What's *not* in scope (yet) for this skeleton:

* Real base commits / real test patches. The seed ships placeholders so
  every row will currently be SKIPped. Populating the seed with the
  actual SWE-bench Verified fields is the next concrete step before any
  real evaluation runs.
* Diff-vs-``expected_patch_summary`` semantic matching. The brief lists
  it as optional; we capture ``diff_lines`` and leave the matcher
  for a follow-up.

The pipeline-cloning / pipeline-running / pytest-running steps are
factored into module-level helpers so the test suite can monkeypatch
them without touching the public API. See ``test_runner.py``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from orchestration.orchestrator.state import OrchestratorState

# ----- module-level helpers (monkeypatchable from tests) ------------------

#: Default location for cloned eval worktrees. Override per-run by
#: monkeypatching the helper, not by editing this constant.
DEFAULT_WORKTREE_ROOT = Path("/tmp/eval_worktrees")


async def _clone_repo(url: str, dest: Path) -> None:
    """Clone ``url`` into ``dest`` and return. Raises ``CalledProcessError``."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _do() -> None:
        subprocess.run(
            ["git", "clone", "--quiet", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )

    await asyncio.to_thread(_do)


async def _checkout(worktree: Path, sha: str) -> None:
    """Hard-reset ``worktree`` to ``sha``."""

    def _do() -> None:
        subprocess.run(
            ["git", "reset", "--hard", sha],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        )

    await asyncio.to_thread(_do)


async def _run_pipeline(state: OrchestratorState) -> None:
    """Drive the orchestration flow against ``state.worktree_path``.

    Imported lazily so the module loads cleanly even when crewai /
    sqlalchemy bits aren't initialised (e.g. in unit tests).
    """
    from orchestration.orchestrator.flow import OrchestratorFlow

    flow = OrchestratorFlow(initial_state=state)
    await flow.kickoff_async()


async def _run_pytest(worktree: Path, test_patch: str) -> int:
    """Apply ``test_patch`` in ``worktree`` and run pytest; return exit code.

    Return value semantics:
        0  → test patch applied + tests passed (pipeline edit was sufficient)
        != 0 → patch failed to apply, or tests still fail

    This skeleton runs ``pytest -q`` in the worktree after best-effort
    patch application. ``git apply`` failures are caught and reported as
    a non-zero exit so the row is marked FAIL rather than crashing the
    runner.
    """

    def _do() -> int:
        # Apply the gold test patch.
        apply = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=worktree,
            input=test_patch,
            capture_output=True,
            text=True,
            check=False,
        )
        if apply.returncode != 0:
            # Patch didn't apply cleanly → can't verify the pipeline's
            # edit against the gold tests. Surface this as a non-zero
            # exit so the row is FAILed with a clear reason.
            return apply.returncode or 1
        # Run the test suite. We don't gate on a specific test selector
        # yet — once FAIL_TO_PASS / PASS_TO_PASS columns land in the
        # seed we should narrow this to just those nodes.
        result = subprocess.run(
            ["pytest", "-q", "--no-header"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode

    return await asyncio.to_thread(_do)


async def _pipeline_diff_lines(worktree: Path) -> int:
    """Number of added+removed lines the pipeline's edit produced."""

    def _do() -> int:
        result = subprocess.run(
            ["git", "diff", "--no-color", "--shortstat"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        # shortstat output: "<n> files changed, <n> insertions(+), <n> deletions(-)"
        total = 0
        for chunk in result.stdout.split(","):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            if digits:
                total += int(digits)
        return total

    return await asyncio.to_thread(_do)


async def _cleanup_worktree(worktree: Path) -> None:
    """Best-effort cleanup; never raise."""

    def _do() -> None:
        shutil.rmtree(worktree, ignore_errors=True)

    # Cleanup is best-effort by contract. Broad except is intentional so
    # an OS hiccup during rmtree doesn't poison the next eval row.
    try:
        await asyncio.to_thread(_do)
    except Exception:  # noqa: BLE001, S110
        pass


# ----- public API ---------------------------------------------------------

#: Type alias for the trio of injectable helpers. Tests monkeypatch the
#: module-level functions individually rather than passing this object
#: through, but the alias documents the contract.
RunnerHooks = Callable[
    [str, Path, OrchestratorState, str], Awaitable[tuple[int, int]]
]


def load_seeds(path: str | Path = "tests/eval/swebench_verified_subset.jsonl") -> list[dict]:
    """Read the JSONL seed file and return a list of issue dicts.

    Each dict carries the eight schema fields documented in Task 35:
    ``instance_id``, ``repo``, ``base_commit``, ``problem_statement``,
    ``test_patch``, ``expected_patch_summary``, ``difficulty``,
    ``estimated_minutes``.
    """
    seeds: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                # Tolerate trailing blank lines; skip them silently.
                continue
            try:
                seeds.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"seed file {path!s} line {line_no}: invalid JSON: {exc}"
                ) from exc
    return seeds


async def run_one_issue(
    seed: dict,
    *,
    worktree_root: Path | None = None,
) -> dict:
    """Run the eval pipeline for a single SWE-bench Verified issue.

    Returns one of three result shapes:

    * ``{"instance_id": ..., "status": "skip", "reason": "no test_patch"}``
      — when ``test_patch`` is empty (current seed behaviour).
    * ``{"instance_id": ..., "status": "fail", "reason": ..., ...}``
      — when the pipeline crashed before verification, or pytest failed.
    * ``{"instance_id": ..., "status": "pass", "duration_s": ..., "diff_lines": ...}``
      — when pytest exited 0 against the pipeline's edit + the gold
      test_patch.
    """
    instance_id = seed["instance_id"]
    started = time.monotonic()

    # 1. Skip on empty test_patch (seed has these by design — see Task 35).
    test_patch = seed.get("test_patch") or ""
    if not test_patch.strip():
        return {
            "instance_id": instance_id,
            "status": "skip",
            "reason": "no test_patch",
            "duration_s": round(time.monotonic() - started, 3),
        }

    # 2. Clone + checkout at base_commit.
    root = worktree_root or DEFAULT_WORKTREE_ROOT
    worktree = root / instance_id.replace("/", "__")
    url = f"https://github.com/{seed['repo']}"
    base_commit = seed["base_commit"]

    try:
        await _clone_repo(url, worktree)
        await _checkout(worktree, base_commit)
    except Exception as exc:  # noqa: BLE001
        return {
            "instance_id": instance_id,
            "status": "fail",
            "reason": f"clone/checkout failed: {exc!r}",
            "duration_s": round(time.monotonic() - started, 3),
        }

    # 3. Run the orchestration pipeline against the freshly-checked-out
    #    worktree. The flow needs project_id + task_id UUIDs; we mint
    #    per-instance ones so concurrent runs don't collide.
    state = OrchestratorState(
        run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        task_external_id=instance_id,
        task_title=f"SWE-bench Verified — {instance_id}",
        task_description=seed.get("problem_statement") or "",
        worktree_path=str(worktree),
    )

    try:
        await _run_pipeline(state)
    except Exception as exc:  # noqa: BLE001
        await _cleanup_worktree(worktree)
        return {
            "instance_id": instance_id,
            "status": "fail",
            "reason": f"pipeline crashed: {exc!r}",
            "duration_s": round(time.monotonic() - started, 3),
        }

    # 4. Verify: apply gold test_patch and run pytest.
    diff_lines = await _pipeline_diff_lines(worktree)
    pytest_exit = await _run_pytest(worktree, test_patch)
    await _cleanup_worktree(worktree)

    duration = round(time.monotonic() - started, 3)
    if pytest_exit == 0:
        return {
            "instance_id": instance_id,
            "status": "pass",
            "duration_s": duration,
            "diff_lines": diff_lines,
        }
    return {
        "instance_id": instance_id,
        "status": "fail",
        "reason": f"pytest exited {pytest_exit}",
        "duration_s": duration,
        "diff_lines": diff_lines,
    }


async def run_all(
    seeds: list[dict],
    *,
    worktree_root: Path | None = None,
    results_dir: str | Path = "tests/eval/results",
) -> dict:
    """Run every seed in sequence and aggregate pass/fail/skip stats.

    Writes a timestamped JSON report under ``results_dir`` and prints
    a one-line summary to stdout. Concurrency is intentionally serial
    in this skeleton — parallel SWE-bench eval needs dedicated worktree
    isolation that we don't have yet, and serial keeps the failure
    signal easy to read.
    """
    results: list[dict] = []
    for seed in seeds:
        result = await run_one_issue(seed, worktree_root=worktree_root)
        results.append(result)
        # Per-row stdout trace — cheap and lets `tee` capture it.
        print(
            f"[eval] {result['instance_id']}: {result['status']}"
            + (f" ({result.get('reason', '')})" if result.get("reason") else "")
        )

    total = len(results)
    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")
    pass_rate = (passed / total) if total else 0.0

    summary = {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pass_rate": round(pass_rate, 4),
        "started_at": datetime.now(UTC).isoformat(),
        "results": results,
    }

    out_dir = Path(results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{ts}.json"

    def _write() -> None:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)

    await asyncio.to_thread(_write)

    print(
        f"[eval] summary: {passed}/{total} passed "
        f"({skipped} skipped, {failed} failed) — pass_rate={summary['pass_rate']:.2%} "
        f"→ {out_path}"
    )
    return summary


def main() -> dict:
    """Synchronous entry point used by ``python -m tests.eval.runner``.

    Reads the seed file at the conventional path and runs every row.
    Returns the summary dict so callers (tests, notebooks) can inspect
    it without re-reading the JSON.
    """
    return asyncio.run(run_all(load_seeds()))


if __name__ == "__main__":
    main()

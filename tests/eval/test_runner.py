"""Tests for ``tests.eval.runner`` (M5/Task 36).

These tests verify the eval runner's structure — they intentionally
don't reach out to GitHub, don't run the real pipeline, and don't
invoke pytest against real worktrees. The runner's three "real"
side effects (clone, run flow, run pytest) are factored into
module-level helpers in ``runner`` and are monkeypatched here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.eval import runner

SEED_PATH = Path("tests/eval/swebench_verified_subset.jsonl")


# ----- fixtures -----------------------------------------------------------


@pytest.fixture
def seed() -> dict:
    """A single, well-formed SWE-bench Verified seed row."""
    return {
        "instance_id": "owner__repo-1234",
        "repo": "owner/repo",
        "base_commit": "abc123def456",
        "problem_statement": "When the user does X, Y crashes because Z.",
        "test_patch": "diff --git a/tests/test_x.py b/tests/test_x.py\n+assert True\n",
        "expected_patch_summary": "Guard against Z in the X path.",
        "difficulty": "medium",
        "estimated_minutes": 30,
    }


@pytest.fixture
def empty_patch_seed() -> dict:
    """Seed with an empty ``test_patch`` — current shape of every row in the seed file."""
    return {
        "instance_id": "owner__repo-empty",
        "repo": "owner/repo",
        "base_commit": "abc123def456",
        "problem_statement": "...",
        "test_patch": "",
        "expected_patch_summary": "...",
        "difficulty": "easy",
        "estimated_minutes": 10,
    }


@pytest.fixture
def patch_runner(monkeypatch) -> AsyncIterator[None]:
    """Disable every module-level helper that touches the outside world.

    ``_clone_repo``, ``_checkout``, ``_run_pipeline``, ``_run_pytest``,
    ``_pipeline_diff_lines``, and ``_cleanup_worktree`` are all
    replaced with no-op async functions so the runner's control flow
    is exercised without I/O. Tests that need richer behaviour
    override the individual helpers themselves.
    """

    async def _noop(*args, **kwargs):
        return None

    async def _zero_diff(*args, **kwargs):
        return 0

    async def _zero_exit(*args, **kwargs):
        return 0

    monkeypatch.setattr(runner, "_clone_repo", _noop)
    monkeypatch.setattr(runner, "_checkout", _noop)
    monkeypatch.setattr(runner, "_run_pipeline", _noop)
    monkeypatch.setattr(runner, "_run_pytest", _zero_exit)
    monkeypatch.setattr(runner, "_pipeline_diff_lines", _zero_diff)
    monkeypatch.setattr(runner, "_cleanup_worktree", _noop)
    yield


# ----- load_seeds ---------------------------------------------------------


def test_load_seeds_returns_ten_items() -> None:
    """The shipped seed file must contain exactly ten rows."""
    seeds = runner.load_seeds(SEED_PATH)
    assert len(seeds) == 10, f"expected 10 seed rows, got {len(seeds)}"


def test_load_seeds_keys_match_schema() -> None:
    """Every row carries the eight schema fields documented in Task 35."""
    expected_keys = {
        "instance_id",
        "repo",
        "base_commit",
        "problem_statement",
        "test_patch",
        "expected_patch_summary",
        "difficulty",
        "estimated_minutes",
    }
    seeds = runner.load_seeds(SEED_PATH)
    for row in seeds:
        assert expected_keys.issubset(row.keys()), (
            f"row {row.get('instance_id', '?')} missing keys: "
            f"{expected_keys - set(row.keys())}"
        )


def test_load_seeds_accepts_blank_lines(tmp_path: Path) -> None:
    """Trailing blank lines must not break parsing."""
    p = tmp_path / "subset.jsonl"
    p.write_text(
        json.dumps({"instance_id": "a__b-1", "test_patch": "x"}) + "\n"
        "\n"  # trailing blank line
        "   \n",  # whitespace-only line
        encoding="utf-8",
    )
    seeds = runner.load_seeds(p)
    assert seeds == [{"instance_id": "a__b-1", "test_patch": "x"}]


def test_load_seeds_raises_on_malformed_line(tmp_path: Path) -> None:
    """Bad JSON must raise ValueError, not crash with bare JSONDecodeError."""
    p = tmp_path / "bad.jsonl"
    p.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        runner.load_seeds(p)


# ----- run_one_issue: skip path -------------------------------------------


async def test_run_one_issue_skips_on_empty_test_patch(
    empty_patch_seed: dict, patch_runner
) -> None:
    """Empty ``test_patch`` → ``status == "skip"`` and no pipeline call."""
    result = await runner.run_one_issue(empty_patch_seed)
    assert result["status"] == "skip"
    assert result["reason"] == "no test_patch"
    assert result["instance_id"] == "owner__repo-empty"
    assert "duration_s" in result


async def test_run_one_issue_treats_whitespace_test_patch_as_empty(
    patch_runner,
) -> None:
    """A whitespace-only ``test_patch`` must also short-circuit to skip."""
    seed = {
        "instance_id": "owner__repo-ws",
        "repo": "owner/repo",
        "base_commit": "abc",
        "problem_statement": "x",
        "test_patch": "   \n\t  ",
        "expected_patch_summary": "x",
        "difficulty": "easy",
        "estimated_minutes": 5,
    }
    result = await runner.run_one_issue(seed)
    assert result["status"] == "skip"
    assert result["reason"] == "no test_patch"


async def test_run_one_issue_skips_without_invoking_pipeline(
    empty_patch_seed: dict, monkeypatch
) -> None:
    """The pipeline must never be called when we short-circuit on skip."""
    calls: list[tuple] = []

    async def _spy(state):
        calls.append(("pipeline", state))

    monkeypatch.setattr(runner, "_run_pipeline", _spy)
    await runner.run_one_issue(empty_patch_seed)
    assert calls == [], "pipeline should not run for skip path"


# ----- run_one_issue: pass/fail path (mocked) ----------------------------


async def test_run_one_issue_passes_when_pytest_exits_zero(
    seed: dict, patch_runner
) -> None:
    """Real-shaped seed + green pytest → ``status == "pass"``."""
    result = await runner.run_one_issue(seed)
    assert result["status"] == "pass"
    assert result["instance_id"] == "owner__repo-1234"
    assert result["diff_lines"] == 0  # from the patch_runner fixture
    assert isinstance(result["duration_s"], float)


async def test_run_one_issue_fails_when_pytest_nonzero(
    seed: dict, monkeypatch, patch_runner
) -> None:
    """Non-zero pytest exit → ``status == "fail"`` with a clear reason."""
    async def _fail_pytest(*args, **kwargs):
        return 1

    monkeypatch.setattr(runner, "_run_pytest", _fail_pytest)
    result = await runner.run_one_issue(seed)
    assert result["status"] == "fail"
    assert "pytest exited 1" in result["reason"]
    assert "diff_lines" in result


async def test_run_one_issue_fails_on_clone_error(
    seed: dict, monkeypatch
) -> None:
    """Clone failure must surface as FAIL, not crash the runner."""
    async def _broken_clone(url, dest):
        raise RuntimeError("network down")

    async def _should_not_run(*args, **kwargs):
        raise AssertionError("should not reach pipeline after clone failure")

    monkeypatch.setattr(runner, "_clone_repo", _broken_clone)
    monkeypatch.setattr(runner, "_checkout", _should_not_run)
    monkeypatch.setattr(runner, "_run_pipeline", _should_not_run)

    result = await runner.run_one_issue(seed)
    assert result["status"] == "fail"
    assert "clone/checkout failed" in result["reason"]
    assert "network down" in result["reason"]


async def test_run_one_issue_fails_on_pipeline_crash(
    seed: dict, monkeypatch, patch_runner
) -> None:
    """Pipeline exception must surface as FAIL with the exception details."""

    async def _explode(state):
        raise RuntimeError("planner oom")

    monkeypatch.setattr(runner, "_run_pipeline", _explode)
    result = await runner.run_one_issue(seed)
    assert result["status"] == "fail"
    assert "pipeline crashed" in result["reason"]
    assert "planner oom" in result["reason"]


# ----- run_all ------------------------------------------------------------


async def test_run_all_aggregates_pass_fail_skip(
    tmp_path: Path, monkeypatch
) -> None:
    """``run_all`` must aggregate stats and write a JSON report file."""
    pass_seed = {
        "instance_id": "owner__repo-1", "repo": "o/r", "base_commit": "c",
        "problem_statement": "p", "test_patch": "diff --git a t\n+x\n",
        "expected_patch_summary": "s", "difficulty": "easy", "estimated_minutes": 1,
    }
    fail_seed = {**pass_seed, "instance_id": "owner__repo-2"}
    skip_seed = {**pass_seed, "instance_id": "owner__repo-3", "test_patch": ""}

    async def _zero(*a, **k):
        return 0

    async def _one(*a, **k):
        return 1

    async def _zero_diff(*a, **k):
        return 0

    # pytest exits 0 for pass_seed, 1 for fail_seed; skip never reaches it.
    async def _selective_pytest(worktree, patch):
        if patch.strip():
            return 0  # both real patches pass
        return 0

    monkeypatch.setattr(runner, "_clone_repo", _zero)
    monkeypatch.setattr(runner, "_checkout", _zero)
    monkeypatch.setattr(runner, "_run_pipeline", _zero)
    monkeypatch.setattr(runner, "_run_pytest", _selective_pytest)
    monkeypatch.setattr(runner, "_pipeline_diff_lines", _zero_diff)
    monkeypatch.setattr(runner, "_cleanup_worktree", _zero)

    # Now patch fail_seed specifically by hooking a per-instance shim
    # that keys on instance_id.
    original_pytest = runner._run_pytest

    async def _by_instance(worktree, patch):
        # The runner doesn't pass instance_id to _run_pytest, but the
        # caller does pass it via the seed; we can inspect worktree
        # path which the runner constructs as ``root / instance_id``.
        if "owner__repo-2" in str(worktree):
            return 1
        return await original_pytest(worktree, patch)

    monkeypatch.setattr(runner, "_run_pytest", _by_instance)

    summary = await runner.run_all(
        [pass_seed, fail_seed, skip_seed],
        results_dir=tmp_path / "results",
    )

    assert summary["total"] == 3
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert len(summary["results"]) == 3
    assert summary["started_at"]

    # JSON file must be written under results_dir.
    written = list((tmp_path / "results").glob("*.json"))
    assert len(written) == 1, f"expected one result file, got {written}"
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload == summary


async def test_run_all_creates_results_dir(tmp_path: Path, monkeypatch) -> None:
    """``run_all`` must create ``results_dir`` if it doesn't exist."""
    empty_seed = {
        "instance_id": "o__r-1", "repo": "o/r", "base_commit": "c",
        "problem_statement": "p", "test_patch": "",
        "expected_patch_summary": "s", "difficulty": "easy", "estimated_minutes": 1,
    }

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(runner, "_clone_repo", _noop)
    monkeypatch.setattr(runner, "_checkout", _noop)
    monkeypatch.setattr(runner, "_run_pipeline", _noop)
    monkeypatch.setattr(runner, "_run_pytest", _noop)
    monkeypatch.setattr(runner, "_pipeline_diff_lines", _noop)
    monkeypatch.setattr(runner, "_cleanup_worktree", _noop)

    out = tmp_path / "fresh" / "results"
    assert not out.exists()
    summary = await runner.run_all([empty_seed], results_dir=out)
    assert out.exists()
    assert summary["total"] == 1
    assert summary["skipped"] == 1

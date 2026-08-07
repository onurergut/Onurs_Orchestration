"""Tests for GitHubPrAdapter using recorded VCR cassettes.

All HTTP interactions are replayed from cassettes under
``tests/adapters/cassettes/`` — no live network calls are made. Polling
intervals are tightened (``poll_interval_s=0.01``) so the await_decision
tests finish in well under a second.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from orchestration.adapters.review.base import Artifacts, Decision, Receipt
from orchestration.adapters.review.github_pr import GitHubPrAdapter
from orchestration.storage.db import session_scope
from orchestration.storage.models import ExternalReceipt, Project, Run, Task

TOKEN = "test-token"
REPO = "example/repo"

# Fixed UUIDs keep the cassette request bodies (which VCR doesn't match on but
# humans read) identical across runs.
RUN_ID = UUID("00000000-0000-0000-0000-000000000001")
TASK_ID = UUID("00000000-0000-0000-0000-000000000002")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000003")


@pytest.fixture(scope="module")
def vcr_config():
    return {
        "match_on": ["method", "scheme", "host", "port", "path"],
    }


def _adapter(**overrides) -> GitHubPrAdapter:
    """Adapter with a fast poll interval so await_decision tests don't sleep."""
    defaults = {"token": TOKEN, "repo": REPO, "poll_interval_s": 0.01}
    defaults.update(overrides)
    return GitHubPrAdapter(**defaults)


async def _seed_run_chain() -> None:
    """Insert a Project + Task + Run so notify_review can resolve project_id."""
    async with session_scope() as session:
        project = Project(
            id=PROJECT_ID,
            name="github-pr-test-project",
            repo_url="https://example.com/github-pr-test.git",
            default_branch="main",
        )
        session.add(project)
        await session.flush()

        task = Task(
            id=TASK_ID,
            project_id=project.id,
            external_id="github-pr-T1",
            source_type="github",
            title="GitHub PR test task",
            description=None,
            status="pending",
        )
        session.add(task)
        await session.flush()

        run = Run(
            id=RUN_ID,
            project_id=project.id,
            task_id=task.id,
            flow_uuid=uuid4(),
            status="running",
            current_stage="review",
        )
        session.add(run)
        await session.flush()


async def _seed_run_with_pr_receipt(pr_number: str) -> None:
    """Insert Project + Task + Run + ExternalReceipt for post_comment."""
    async with session_scope() as session:
        project = Project(
            id=PROJECT_ID,
            name="github-pr-comment-project",
            repo_url="https://example.com/github-pr-comment.git",
            default_branch="main",
        )
        session.add(project)
        await session.flush()

        task = Task(
            id=TASK_ID,
            project_id=project.id,
            external_id="github-pr-T1",
            source_type="github",
            title="GitHub PR comment test task",
            description=None,
            status="pending",
        )
        session.add(task)
        await session.flush()

        run = Run(
            id=RUN_ID,
            project_id=project.id,
            task_id=task.id,
            flow_uuid=uuid4(),
            status="running",
            current_stage="review",
        )
        session.add(run)
        await session.flush()

        receipt = ExternalReceipt(
            id=uuid4(),
            run_id=run.id,
            idempotency_key=f"github_pr:notify:{run.id}",
            adapter_type="github_pr",
            operation="notify",
            external_id=pr_number,
            request_payload={"summary": "seeded"},
            response_payload={"number": pr_number, "head": f"orch/{run.id}"},
            status="succeeded",
        )
        session.add(receipt)
        await session.flush()


# ---------------------------------------------------------------- env / init


def test_init_missing_token_raises(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        GitHubPrAdapter()


def test_init_missing_repo_raises(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    with pytest.raises(ValueError, match="GITHUB_REPO"):
        GitHubPrAdapter()


def test_init_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "abc")
    monkeypatch.setenv("GITHUB_REPO", "foo/bar")
    a = GitHubPrAdapter()
    assert a.token == "abc"
    assert a.repo == "foo/bar"


def test_name_is_github_pr():
    assert GitHubPrAdapter.name == "github_pr"


def test_headers_include_bearer_token():
    adapter = _adapter()
    h = adapter._headers()
    assert h["Authorization"] == f"Bearer {TOKEN}"
    assert h["Accept"] == "application/vnd.github+json"
    assert h["X-GitHub-Api-Version"] == "2022-11-28"


def test_url_prefix():
    adapter = _adapter()
    assert (
        adapter._url("/repos/foo/bar/pulls")
        == "https://api.github.com/repos/foo/bar/pulls"
    )
    assert (
        adapter._url("repos/foo/bar/pulls")
        == "https://api.github.com/repos/foo/bar/pulls"
    )


# ---------------------------------------------------------------- _classify_comment


def test_classify_comment_approve():
    decision = GitHubPrAdapter._classify_comment("lgtm, ship it")
    assert decision is not None
    assert decision.approve is True
    assert decision.reject is False
    assert decision.comment == "lgtm, ship it"


def test_classify_comment_approved_wins_over_reject_substring():
    # "approved" contains "approve" but not "reject"; we expect approve to win.
    decision = GitHubPrAdapter._classify_comment("approved, thanks")
    assert decision is not None
    assert decision.approve is True


def test_classify_comment_reject():
    decision = GitHubPrAdapter._classify_comment("needs more work, reject")
    assert decision is not None
    assert decision.approve is False
    assert decision.reject is True
    assert decision.comment == "needs more work, reject"


def test_classify_comment_denied():
    decision = GitHubPrAdapter._classify_comment("denied — see logs")
    assert decision is not None
    assert decision.approve is False
    assert decision.reject is True


def test_classify_comment_no_keyword_returns_none():
    assert GitHubPrAdapter._classify_comment("just a status update") is None


def test_classify_comment_empty_returns_none():
    assert GitHubPrAdapter._classify_comment("") is None


# ---------------------------------------------------------------- _build_pr_body


def test_build_pr_body_summary_only():
    body = GitHubPrAdapter._build_pr_body(Artifacts(summary="hello"))
    assert "hello" in body
    assert "## Diff" not in body


def test_build_pr_body_with_diff_and_report():
    body = GitHubPrAdapter._build_pr_body(
        Artifacts(
            summary="hello",
            diff="--- a\n+++ b\n-old\n+new",
            test_report={"passed": 3, "failed": 0},
        )
    )
    assert "hello" in body
    assert "## Diff" in body
    assert "+new" in body
    assert "## Test report" in body
    assert '"passed": 3' in body


def test_build_pr_body_truncates_long_input():
    huge_diff = "x" * 70_000
    body = GitHubPrAdapter._build_pr_body(
        Artifacts(summary="summary", diff=huge_diff)
    )
    assert len(body) <= 60_000 + len("\n\n... (truncated)")
    assert "truncated" in body


# ---------------------------------------------------------------- notify_review


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_notify_review_creates_pr():
    await _seed_run_chain()
    adapter = _adapter()
    artifacts = Artifacts(
        summary="summary text",
        diff="--- a/x.py\n+++ b/x.py\n@@\n-old\n+new",
    )
    receipt = await adapter.notify_review(RUN_ID, artifacts)
    assert isinstance(receipt, Receipt)
    assert receipt.id is not None
    # Cassette returns PR number 101.
    assert receipt.external_id == "101"
    # The receipt row must be persisted with adapter_type/external_id set.
    from sqlalchemy import select

    async with session_scope() as session:
        stored = (
            await session.execute(
                select(ExternalReceipt).where(ExternalReceipt.id == receipt.id)
            )
        ).scalar_one()
        assert stored.run_id == RUN_ID
        assert stored.adapter_type == "github_pr"
        assert stored.operation == "notify"
        assert stored.external_id == "101"
        assert stored.status == "succeeded"


@pytest.mark.asyncio
async def test_notify_review_requires_known_run():
    adapter = _adapter()
    unknown = uuid4()
    # Should fail fast on the missing Run before hitting the GitHub API.
    with pytest.raises(ValueError, match=f"run {unknown} not found"):
        await adapter.notify_review(unknown, Artifacts(summary="x"))


# ---------------------------------------------------------------- await_decision


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_await_decision_approve():
    # Cassette: first poll empty, second poll returns "lgtm, ship it".
    adapter = _adapter()
    receipt = Receipt(id=uuid4(), external_id="123")
    decision = await adapter.await_decision(receipt, timeout_s=5)
    assert isinstance(decision, Decision)
    assert decision.approve is True
    assert decision.reject is False
    assert decision.comment == "lgtm, ship it"


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_await_decision_reject():
    # Cassette: first poll empty, second poll returns reject comment.
    adapter = _adapter()
    receipt = Receipt(id=uuid4(), external_id="124")
    decision = await adapter.await_decision(receipt, timeout_s=5)
    assert decision.approve is False
    assert decision.reject is True
    assert "reject" in (decision.comment or "").lower()


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_await_decision_timeout():
    # Cassette: two polls, neither has a decision keyword.
    # `timeout_s=0.025` ensures the wait_for cancels during the second sleep
    # rather than starting a third request (which would miss a cassette entry).
    adapter = _adapter()
    receipt = Receipt(id=uuid4(), external_id="125")
    decision = await adapter.await_decision(receipt, timeout_s=0.025)
    assert decision.approve is False
    assert decision.reject is True
    assert decision.comment is not None
    assert "timeout" in decision.comment


@pytest.mark.asyncio
async def test_await_decision_missing_external_id():
    adapter = _adapter()
    receipt = Receipt(id=uuid4(), external_id=None)
    decision = await adapter.await_decision(receipt, timeout_s=1)
    assert decision.approve is False
    assert decision.reject is True
    assert decision.comment == "missing receipt external_id"


# ---------------------------------------------------------------- post_comment


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_post_comment_hits_pr_endpoint():
    await _seed_run_with_pr_receipt("126")
    adapter = _adapter()
    result = await adapter.post_comment(RUN_ID, "build failed, retrying")
    assert result is None


@pytest.mark.asyncio
async def test_post_comment_without_receipt_raises():
    adapter = _adapter()
    unknown = uuid4()
    with pytest.raises(ValueError, match=f"run {unknown} not found"):
        await adapter.post_comment(unknown, "anything")
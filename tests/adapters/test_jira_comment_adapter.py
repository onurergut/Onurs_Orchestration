"""Tests for JiraCommentAdapter using recorded VCR cassettes.

All HTTP interactions are replayed from cassettes under
``tests/adapters/cassettes/`` — no live network calls are made. Polling
intervals are tightened (``poll_interval_s=0.01``) so the await_decision
tests finish in well under a second.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from orchestration.adapters.review.base import Artifacts, Decision, Receipt
from orchestration.adapters.review.jira_comment import JiraCommentAdapter
from orchestration.storage.db import session_scope
from orchestration.storage.models import ExternalReceipt, Project, Run, Task

EMAIL = "test@example.com"
TOKEN = "test-token"
BASE_URL = "https://example.atlassian.net"
PROJECT_KEY = "PROJ"

# Fixed UUIDs keep the cassette request bodies (which VCR doesn't match on
# but humans read) identical across runs.
RUN_ID = UUID("00000000-0000-0000-0000-000000000010")
TASK_ID = UUID("00000000-0000-0000-0000-000000000011")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000012")
ISSUE_KEY = "PROJ-50"


@pytest.fixture(scope="module")
def vcr_config():
    return {
        "match_on": ["method", "scheme", "host", "port", "path"],
    }


def _adapter(**overrides) -> JiraCommentAdapter:
    """Adapter with a fast poll interval so await_decision tests don't sleep."""
    defaults = {
        "email": EMAIL,
        "token": TOKEN,
        "base_url": BASE_URL,
        "project_key": PROJECT_KEY,
        "poll_interval_s": 0.01,
    }
    defaults.update(overrides)
    return JiraCommentAdapter(**defaults)


async def _seed_run_chain(issue_key: str = ISSUE_KEY) -> None:
    """Insert Project + Task + Run so notify_review can resolve the issue key."""
    async with session_scope() as session:
        project = Project(
            id=PROJECT_ID,
            name="jira-comment-test-project",
            repo_url="https://example.com/jira-comment-test.git",
            default_branch="main",
        )
        session.add(project)
        await session.flush()

        task = Task(
            id=TASK_ID,
            project_id=project.id,
            external_id=issue_key,
            source_type="jira",
            title="Jira comment test task",
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


async def _seed_run_with_receipt(issue_key: str = ISSUE_KEY) -> None:
    """Insert Project + Task + Run + ExternalReceipt for post_comment."""
    await _seed_run_chain(issue_key=issue_key)
    async with session_scope() as session:
        receipt = ExternalReceipt(
            id=uuid4(),
            run_id=RUN_ID,
            idempotency_key=f"jira_comment:notify:{RUN_ID}",
            adapter_type="jira_comment",
            operation="notify",
            external_id=issue_key,
            request_payload={"summary": "seeded"},
            response_payload={"issue_key": issue_key},
            status="succeeded",
        )
        session.add(receipt)
        await session.flush()


# ---------------------------------------------------------------- env / init


def test_init_missing_email_raises(monkeypatch):
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    with pytest.raises(ValueError, match="JIRA_EMAIL"):
        JiraCommentAdapter()


def test_init_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "env@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "env-token")
    monkeypatch.setenv("JIRA_BASE_URL", "https://env.atlassian.net")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ENV")
    a = JiraCommentAdapter()
    assert a.email == "env@example.com"
    assert a.token == "env-token"
    assert a.base_url == "https://env.atlassian.net"
    assert a.project_key == "ENV"


def test_name_is_jira_comment():
    assert JiraCommentAdapter.name == "jira_comment"


# ---------------------------------------------------------------- _classify_comment


def test_classify_comment_approve():
    decision = JiraCommentAdapter._classify_comment("lgtm, ship it")
    assert decision is not None
    assert decision.approve is True
    assert decision.reject is False
    assert decision.comment == "lgtm, ship it"


def test_classify_comment_approved_wins_over_reject_substring():
    # "approved" contains "approve" but not "reject"; we expect approve to win.
    decision = JiraCommentAdapter._classify_comment("approved, thanks")
    assert decision is not None
    assert decision.approve is True


def test_classify_comment_reject():
    decision = JiraCommentAdapter._classify_comment("needs more work, reject")
    assert decision is not None
    assert decision.approve is False
    assert decision.reject is True


def test_classify_comment_denied():
    decision = JiraCommentAdapter._classify_comment("denied — see logs")
    assert decision is not None
    assert decision.approve is False
    assert decision.reject is True


def test_classify_comment_no_keyword_returns_none():
    assert JiraCommentAdapter._classify_comment("just a status update") is None


def test_classify_comment_empty_returns_none():
    assert JiraCommentAdapter._classify_comment("") is None


# ---------------------------------------------------------------- _build_review_body


def test_build_review_body_summary_only():
    body = JiraCommentAdapter._build_review_body(Artifacts(summary="hello"))
    assert "hello" in body
    assert "Diff:" not in body


def test_build_review_body_with_diff_and_report():
    body = JiraCommentAdapter._build_review_body(
        Artifacts(
            summary="hello",
            diff="--- a\n+++ b\n-old\n+new",
            test_report={"passed": 3, "failed": 0},
        )
    )
    assert "hello" in body
    assert "Diff:" in body
    assert "+new" in body
    assert "Test report:" in body
    assert '"passed": 3' in body
    # Always include decision instructions.
    assert "approve" in body.lower()
    assert "reject" in body.lower()


# ---------------------------------------------------------------- notify_review


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_notify_review_posts_comment_and_persists_receipt():
    await _seed_run_chain()
    adapter = _adapter()
    artifacts = Artifacts(summary="summary text", diff="+x")
    receipt = await adapter.notify_review(RUN_ID, artifacts)
    assert isinstance(receipt, Receipt)
    assert receipt.id is not None
    assert receipt.external_id == ISSUE_KEY

    # Receipt must be persisted with adapter_type/external_id set.
    from sqlalchemy import select

    async with session_scope() as session:
        stored = (
            await session.execute(
                select(ExternalReceipt).where(ExternalReceipt.id == receipt.id)
            )
        ).scalar_one()
        assert stored.run_id == RUN_ID
        assert stored.adapter_type == "jira_comment"
        assert stored.operation == "notify"
        assert stored.external_id == ISSUE_KEY
        assert stored.status == "succeeded"


@pytest.mark.asyncio
async def test_notify_review_requires_known_run():
    adapter = _adapter()
    unknown = uuid4()
    with pytest.raises(ValueError, match=f"run {unknown} not found"):
        await adapter.notify_review(unknown, Artifacts(summary="x"))


# ---------------------------------------------------------------- await_decision


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_await_decision_approve():
    # Cassette: first poll empty, second poll returns "lgtm, ship it".
    adapter = _adapter()
    receipt = Receipt(id=uuid4(), external_id=ISSUE_KEY)
    decision = await adapter.await_decision(receipt, timeout_s=5)
    assert isinstance(decision, Decision)
    assert decision.approve is True
    assert decision.reject is False
    assert decision.comment == "lgtm, ship it"


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_await_decision_reject():
    # Cassette: first poll empty, second poll returns reject comment.
    adapter = _adapter()
    receipt = Receipt(id=uuid4(), external_id="PROJ-51")
    decision = await adapter.await_decision(receipt, timeout_s=5)
    assert decision.approve is False
    assert decision.reject is True
    assert "reject" in (decision.comment or "").lower()


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_await_decision_timeout():
    # Cassette: two polls, neither has a decision keyword.
    # `timeout_s=0.025` ensures the wait_for cancels during the second sleep
    # rather than starting a third request (which would miss a cassette entry).
    adapter = _adapter()
    receipt = Receipt(id=uuid4(), external_id="PROJ-52")
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
async def test_post_comment_hits_issue_comment_endpoint():
    await _seed_run_with_receipt()
    adapter = _adapter()
    result = await adapter.post_comment(RUN_ID, "build failed, retrying")
    assert result is None


@pytest.mark.asyncio
async def test_post_comment_without_receipt_raises():
    # The run exists but has no jira_comment receipt, so post_comment
    # cannot resolve the issue key.
    await _seed_run_chain()
    adapter = _adapter()
    with pytest.raises(ValueError, match="no Jira comment receipt found"):
        await adapter.post_comment(RUN_ID, "anything")

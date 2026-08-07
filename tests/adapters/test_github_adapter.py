"""Tests for GitHubIssuesAdapter using recorded VCR cassettes.

All HTTP interactions are replayed from cassettes under
``tests/adapters/cassettes/`` — no live network calls are made.
"""

from __future__ import annotations

import pytest

from orchestration.adapters.task_source.base import Comment, CreateTask, ExternalTask
from orchestration.adapters.task_source.github import GitHubIssuesAdapter

TOKEN = "test-token"
REPO = "example/repo"


# Override pytest-vcr's vcr_config to drop the ``query`` matcher: cassettes
# store the canonical URL, but the adapter may add harmless params like
# ``per_page``. We only care that method, scheme, host, port and path line up.
@pytest.fixture(scope="module")
def vcr_config():
    return {
        "match_on": ["method", "scheme", "host", "port", "path"],
    }


def _adapter() -> GitHubIssuesAdapter:
    return GitHubIssuesAdapter(token=TOKEN, repo=REPO)


# ---------------------------------------------------------------- env / init


def test_init_missing_token_raises(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        GitHubIssuesAdapter()


def test_init_missing_repo_raises(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    with pytest.raises(ValueError, match="GITHUB_REPO"):
        GitHubIssuesAdapter()


def test_init_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "abc")
    monkeypatch.setenv("GITHUB_REPO", "foo/bar")
    a = GitHubIssuesAdapter()
    assert a.token == "abc"
    assert a.repo == "foo/bar"


def test_name_is_github():
    assert GitHubIssuesAdapter.name == "github"


# ---------------------------------------------------------------- list_tasks


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_github_list_filters_prs():
    adapter = _adapter()
    tasks = await adapter.list_tasks(project_id=None, status="open")
    # 3 items returned by cassette: 2 issues + 1 PR; PR must be filtered out.
    assert len(tasks) == 2
    assert all(isinstance(t, ExternalTask) for t in tasks)
    assert [t.external_id for t in tasks] == ["1", "2"]
    assert tasks[0].title == "First issue"
    assert tasks[1].title == "Second issue"
    # Metadata must surface url, state, labels.
    assert tasks[0].metadata["url"] == "https://github.com/example/repo/issues/1"
    assert tasks[0].metadata["state"] == "open"
    assert tasks[0].metadata["labels"] == ["bug"]
    assert tasks[1].metadata["labels"] == []


# ---------------------------------------------------------------- get_task


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_github_get_task():
    adapter = _adapter()
    task = await adapter.get_task("123")
    assert isinstance(task, ExternalTask)
    assert task.external_id == "123"
    assert task.title == "Issue 123"
    assert task.description == "Body for 123"
    assert task.metadata["url"] == "https://github.com/example/repo/issues/123"
    assert task.metadata["labels"] == ["enhancement"]
    assert task.metadata["user"] == "alice"


# ---------------------------------------------------------------- create_task


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_github_create_task():
    adapter = _adapter()
    payload = CreateTask(title="New issue", description="Body of new issue")
    task = await adapter.create_task(project_id=None, payload=payload)
    assert task.external_id == "42"
    assert task.title == "New issue"
    assert task.description == "Body of new issue"
    assert task.metadata["url"] == "https://github.com/example/repo/issues/42"
    assert task.metadata["state"] == "open"


# ---------------------------------------------------------------- update_status


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_github_update_status():
    adapter = _adapter()
    # Should not raise; cassette confirms the PATCH payload shape.
    await adapter.update_status("123", "closed")


# ---------------------------------------------------------------- add_comment


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_github_add_comment():
    adapter = _adapter()
    comment = await adapter.add_comment("123", "Looks good to me")
    assert isinstance(comment, Comment)
    assert comment.body == "Looks good to me"
    assert comment.external_id == "999"


# ---------------------------------------------------------------- auth header


def test_headers_include_bearer_token():
    adapter = _adapter()
    h = adapter._headers()
    assert h["Authorization"] == f"Bearer {TOKEN}"
    assert h["Accept"] == "application/vnd.github+json"
    assert h["X-GitHub-Api-Version"] == "2022-11-28"


def test_url_prefix():
    adapter = _adapter()
    assert adapter._url("/repos/foo/bar/issues") == "https://api.github.com/repos/foo/bar/issues"
    assert adapter._url("repos/foo/bar/issues") == "https://api.github.com/repos/foo/bar/issues"


def test_normalize_state():
    assert GitHubIssuesAdapter._normalize_state(None) == "open"
    assert GitHubIssuesAdapter._normalize_state("open") == "open"
    assert GitHubIssuesAdapter._normalize_state("closed") == "closed"
    assert GitHubIssuesAdapter._normalize_state("done") == "closed"
    assert GitHubIssuesAdapter._normalize_state("all") == "all"
    assert GitHubIssuesAdapter._normalize_state("weird") == "open"

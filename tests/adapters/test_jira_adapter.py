"""Tests for JiraAdapter using recorded VCR cassettes.

All HTTP interactions are replayed from cassettes under
``tests/adapters/cassettes/`` — no live network calls are made.
"""

from __future__ import annotations

import base64

import pytest

from orchestration.adapters.task_source.base import Comment, CreateTask, ExternalTask
from orchestration.adapters.task_source.jira import JiraAdapter

EMAIL = "test@example.com"
TOKEN = "test-token"
BASE_URL = "https://example.atlassian.net"
PROJECT_KEY = "PROJ"
DONE_TRANSITION_ID = "31"


def _adapter(**overrides) -> JiraAdapter:
    defaults = {
        "email": EMAIL,
        "token": TOKEN,
        "base_url": BASE_URL,
        "project_key": PROJECT_KEY,
        "done_transition_id": DONE_TRANSITION_ID,
    }
    defaults.update(overrides)
    return JiraAdapter(**defaults)


@pytest.fixture(scope="module")
def vcr_config():
    return {
        "match_on": ["method", "scheme", "host", "port", "path"],
    }


# ---------------------------------------------------------------- env / init


def test_init_missing_email_raises(monkeypatch):
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    with pytest.raises(ValueError, match="JIRA_EMAIL"):
        JiraAdapter()


def test_init_missing_token_raises(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "x")
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="JIRA_API_TOKEN"):
        JiraAdapter()


def test_init_missing_base_url_raises(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "x")
    monkeypatch.setenv("JIRA_API_TOKEN", "x")
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="JIRA_BASE_URL"):
        JiraAdapter()


def test_init_missing_project_key_raises(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "x")
    monkeypatch.setenv("JIRA_API_TOKEN", "x")
    monkeypatch.setenv("JIRA_BASE_URL", "https://x.atlassian.net")
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    with pytest.raises(ValueError, match="JIRA_PROJECT_KEY"):
        JiraAdapter()


def test_init_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "env@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "env-token")
    monkeypatch.setenv("JIRA_BASE_URL", "https://env.atlassian.net")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ENV")
    a = JiraAdapter()
    assert a.email == "env@example.com"
    assert a.token == "env-token"
    assert a.base_url == "https://env.atlassian.net"
    assert a.project_key == "ENV"
    # Trailing slash should be normalised away so URL composition is predictable.
    monkeypatch.setenv("JIRA_BASE_URL", "https://env.atlassian.net/")
    b = JiraAdapter()
    assert b.base_url == "https://env.atlassian.net"


def test_name_is_jira():
    assert JiraAdapter.name == "jira"


# ---------------------------------------------------------------- auth header


def test_auth_header_is_basic_base64():
    adapter = _adapter()
    expected_raw = f"{EMAIL}:{TOKEN}".encode()
    expected_encoded = base64.b64encode(expected_raw).decode("ascii")
    assert adapter._auth_header() == f"Basic {expected_encoded}"


def test_headers_include_basic_auth_and_json():
    adapter = _adapter()
    h = adapter._headers()
    assert h["Authorization"].startswith("Basic ")
    assert h["Accept"] == "application/json"
    assert h["Content-Type"] == "application/json"


def test_url_prefix():
    adapter = _adapter()
    assert (
        adapter._url("/rest/api/3/search")
        == f"{BASE_URL}/rest/api/3/search"
    )
    assert (
        adapter._url("rest/api/3/search")
        == f"{BASE_URL}/rest/api/3/search"
    )


# ---------------------------------------------------------------- ADF helpers


def test_to_adf_wraps_text_in_paragraph():
    adf = JiraAdapter._to_adf("hello")
    assert adf == {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "hello"}],
            }
        ],
    }


def test_to_adf_handles_empty_string():
    adf = JiraAdapter._to_adf("")
    assert adf["content"][0]["content"][0]["text"] == ""


def test_from_adf_extracts_text():
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "hello world"}],
            }
        ],
    }
    assert JiraAdapter._from_adf(adf) == "hello world"


def test_from_adf_concatenates_nested_text():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "abc"},
                {"type": "text", "text": "def"},
            ]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "ghi"},
            ]},
        ],
    }
    assert JiraAdapter._from_adf(adf) == "abcdefghi"


def test_from_adf_handles_non_dict():
    assert JiraAdapter._from_adf(None) == ""
    assert JiraAdapter._from_adf("plain") == ""
    assert JiraAdapter._from_adf({}) == ""


def test_from_adf_round_trip():
    text = "looks good to me"
    assert JiraAdapter._from_adf(JiraAdapter._to_adf(text)) == text


# ---------------------------------------------------------------- list_tasks


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_list_tasks():
    adapter = _adapter()
    tasks = await adapter.list_tasks(project_id=None, status=None)
    assert len(tasks) == 2
    assert all(isinstance(t, ExternalTask) for t in tasks)
    assert [t.external_id for t in tasks] == ["PROJ-1", "PROJ-2"]
    assert tasks[0].title == "First ticket"
    # ADF description should be flattened back to plain text.
    assert tasks[0].description == "First body"
    # status metadata must surface both name and category.
    assert tasks[0].metadata["status"] == "To Do"
    assert tasks[0].metadata["status_category"] == "new"
    assert tasks[0].metadata["labels"] == ["urgent"]


# ---------------------------------------------------------------- get_task


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_get_task():
    adapter = _adapter()
    task = await adapter.get_task("PROJ-1")
    assert isinstance(task, ExternalTask)
    assert task.external_id == "PROJ-1"
    assert task.title == "First ticket"
    assert task.description == "First body"
    assert task.metadata["status"] == "To Do"
    assert task.metadata["priority"] == "Medium"
    assert task.metadata["assignee"] == "Alice"


# ---------------------------------------------------------------- create_task


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_create_task():
    adapter = _adapter()
    payload = CreateTask(title="New ticket", description="New body")
    task = await adapter.create_task(project_id=None, payload=payload)
    assert task.external_id == "PROJ-100"
    assert task.title == "New ticket"
    assert task.description == "New body"
    assert task.metadata["status"] == "To Do"


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_create_task_with_labels():
    adapter = _adapter()
    payload = CreateTask(
        title="With labels",
        description="Body",
        metadata={"labels": ["bug", "p1"]},
    )
    task = await adapter.create_task(project_id=None, payload=payload)
    assert task.external_id == "PROJ-101"
    assert task.metadata["labels"] == ["bug", "p1"]


# ---------------------------------------------------------------- update_status


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_update_status_uses_done_transition_id():
    adapter = _adapter()
    # Should not raise; cassette confirms POST to transitions with
    # {"transition": {"id": "31"}}.
    await adapter.update_status("PROJ-1", "done")


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_update_status_falls_back_to_name_lookup():
    # No done_transition_id, status "in progress" — adapter should fetch
    # transitions and pick the one named "In Progress".
    adapter = _adapter(done_transition_id=None)
    await adapter.update_status("PROJ-1", "in progress")


@pytest.mark.asyncio
async def test_jira_update_status_raises_when_no_match(monkeypatch):
    adapter = _adapter(done_transition_id=None)
    # Stub out _request so we can return transitions with no matching name.
    from unittest.mock import AsyncMock

    adapter._request = AsyncMock(  # type: ignore[method-assign]
        return_value={"transitions": [{"id": "1", "name": "Done"}]}
    )
    with pytest.raises(ValueError, match="no Jira transition matched"):
        await adapter.update_status("PROJ-1", "weird-status")


# ---------------------------------------------------------------- add_comment


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_jira_add_comment():
    adapter = _adapter()
    comment = await adapter.add_comment("PROJ-1", "Looks good")
    assert isinstance(comment, Comment)
    assert comment.body == "Looks good"
    assert comment.external_id == "10001"


# ---------------------------------------------------------------- extract comment body helper


def test_extract_comment_body_from_adf():
    adf = JiraAdapter._to_adf("hello")
    assert JiraAdapter._extract_comment_body(adf) == "hello"


def test_extract_comment_body_from_plain_string():
    assert JiraAdapter._extract_comment_body("plain") == "plain"


def test_extract_comment_body_from_other():
    assert JiraAdapter._extract_comment_body(None) == ""
    assert JiraAdapter._extract_comment_body(123) == ""

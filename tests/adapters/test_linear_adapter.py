"""Tests for LinearAdapter using recorded VCR cassettes.

All HTTP interactions are replayed from cassettes under
``tests/adapters/cassettes/`` — no live network calls are made.
"""

from __future__ import annotations

import pytest

from orchestration.adapters.task_source.base import Comment, CreateTask, ExternalTask
from orchestration.adapters.task_source.linear import (
    INTERNAL_TO_LINEAR_STATE,
    LinearAdapter,
    _looks_like_uuid,
)

API_KEY = "lin_api_test-key"
TEAM_ID = "00000000-0000-0000-0000-000000000001"


def _adapter(**overrides) -> LinearAdapter:
    defaults = {"api_key": API_KEY, "team_id": TEAM_ID}
    defaults.update(overrides)
    return LinearAdapter(**defaults)


@pytest.fixture(scope="module")
def vcr_config():
    # Drop the ``query`` matcher: GraphQL request bodies are large and
    # variable-key order in JSON encoding is stable but we only need to
    # match on method + scheme + host + port + path.
    return {
        "match_on": ["method", "scheme", "host", "port", "path"],
    }


# ---------------------------------------------------------------- env / init


def test_init_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("LINEAR_TEAM_ID", raising=False)
    with pytest.raises(ValueError, match="LINEAR_API_KEY"):
        LinearAdapter()


def test_init_missing_team_id_raises(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "x")
    monkeypatch.delenv("LINEAR_TEAM_ID", raising=False)
    with pytest.raises(ValueError, match="LINEAR_TEAM_ID"):
        LinearAdapter()


def test_init_from_env(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_xyz")
    monkeypatch.setenv("LINEAR_TEAM_ID", "team-uuid")
    a = LinearAdapter()
    assert a.api_key == "lin_api_xyz"
    assert a.team_id == "team-uuid"


def test_name_is_linear():
    assert LinearAdapter.name == "linear"


# ---------------------------------------------------------------- headers / uuid


def test_headers_use_raw_api_key():
    a = _adapter()
    h = a._headers()
    assert h["Authorization"] == API_KEY
    assert h["Content-Type"] == "application/json"


def test_looks_like_uuid_recognises_canonical():
    assert _looks_like_uuid("00000000-0000-0000-0000-000000000001")
    assert _looks_like_uuid("ABCDEF12-3456-7890-ABCD-EF1234567890")
    assert not _looks_like_uuid("ENG-123")
    assert not _looks_like_uuid("Done")
    assert not _looks_like_uuid("")


# ---------------------------------------------------------------- vocabulary


def test_internal_to_linear_state_lookup():
    assert LinearAdapter._internal_to_linear_state_name("done") == "Done"
    assert LinearAdapter._internal_to_linear_state_name("in_progress") == "In Progress"
    assert LinearAdapter._internal_to_linear_state_name("todo") == "Todo"
    assert LinearAdapter._internal_to_linear_state_name("canceled") == "Canceled"
    assert LinearAdapter._internal_to_linear_state_name("DONE") == "Done"
    assert LinearAdapter._internal_to_linear_state_name("unknown") is None


def test_linear_state_name_to_internal():
    assert LinearAdapter._linear_state_name_to_internal("Done") == "done"
    assert LinearAdapter._linear_state_name_to_internal("In Progress") == "in_progress"
    assert LinearAdapter._linear_state_name_to_internal("Canceled") == "canceled"
    assert LinearAdapter._linear_state_name_to_internal("Unknown") is None


def test_vocabulary_table_is_consistent():
    # Every internal status maps to a non-empty Linear name and back again.
    for internal, linear in INTERNAL_TO_LINEAR_STATE.items():
        assert linear, f"empty Linear name for {internal!r}"
        assert LinearAdapter._linear_state_name_to_internal(linear) == internal


# ---------------------------------------------------------------- list_tasks


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_linear_list_tasks():
    adapter = _adapter()
    tasks = await adapter.list_tasks(project_id=None, status=None)
    assert len(tasks) == 2
    assert all(isinstance(t, ExternalTask) for t in tasks)
    assert [t.external_id for t in tasks] == ["ENG-1", "ENG-2"]
    assert tasks[0].title == "First issue"
    assert tasks[0].description == "First body"
    assert tasks[0].metadata["state"] == "Todo"
    assert tasks[0].metadata["internal_status"] == "todo"
    assert tasks[1].metadata["state"] == "In Progress"
    assert tasks[1].metadata["internal_status"] == "in_progress"


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_linear_list_tasks_with_status_filter():
    # status="in_progress" should be translated to Linear's "In Progress" name.
    adapter = _adapter()
    tasks = await adapter.list_tasks(project_id=None, status="in_progress")
    assert len(tasks) == 1
    assert tasks[0].external_id == "ENG-2"
    assert tasks[0].metadata["state"] == "In Progress"


# ---------------------------------------------------------------- get_task


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_linear_get_task():
    adapter = _adapter()
    task = await adapter.get_task("ENG-1")
    assert isinstance(task, ExternalTask)
    assert task.external_id == "ENG-1"
    assert task.title == "First issue"
    assert task.description == "First body"
    assert task.metadata["state"] == "Todo"
    assert task.metadata["id"] == "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------- create_task


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_linear_create_task():
    adapter = _adapter()
    payload = CreateTask(title="New issue", description="New body")
    task = await adapter.create_task(project_id=None, payload=payload)
    assert task.external_id == "ENG-100"
    assert task.title == "New issue"
    assert task.description == "New body"
    assert task.metadata["state"] == "Todo"


# ---------------------------------------------------------------- update_status


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_linear_update_status_with_state_id():
    # Pass a Linear state UUID directly — bypasses workflowStates lookup.
    adapter = _adapter()
    state_id = "22222222-2222-2222-2222-222222222222"
    await adapter.update_status("ENG-1", state_id)


# ---------------------------------------------------------------- add_comment


@pytest.mark.vcr
@pytest.mark.asyncio
async def test_linear_add_comment():
    adapter = _adapter()
    comment = await adapter.add_comment("ENG-1", "Looks good to me")
    assert isinstance(comment, Comment)
    assert comment.body == "Looks good to me"
    assert comment.external_id == "comment-1"


# ---------------------------------------------------------------- GraphQL error handling


@pytest.mark.asyncio
async def test_request_raises_on_graphql_errors(monkeypatch):
    adapter = _adapter()

    class _StubResponse:
        status_code = 200
        content = b'{"data": null, "errors": [{"message": "bad query"}]}'

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "data": None,
                "errors": [{"message": "bad query"}],
            }

    class _StubClient:
        async def post(self, url, **kwargs):
            return _StubResponse()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(adapter, "_client", _StubClient())
    with pytest.raises(RuntimeError, match="Linear GraphQL error"):
        await adapter._request("query { issues { nodes { id } } }")


@pytest.mark.asyncio
async def test_request_raises_on_http_error(monkeypatch):
    import httpx

    adapter = _adapter()

    class _StubResponse:
        status_code = 401
        content = b"unauthorized"

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                "401", request=httpx.Request("POST", "/"), response=self  # type: ignore[arg-type]
            )

        def json(self):
            return {}

    class _StubClient:
        async def post(self, url, **kwargs):
            return _StubResponse()

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(adapter, "_client", _StubClient())
    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("query { issues { nodes { id } } }")
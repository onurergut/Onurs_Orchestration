"""Tests for the suggestion picker API routes (M5/Task 34)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import insert, select
from tests.conftest import _hash_key

from orchestration.storage.db import get_session_factory
from orchestration.storage.models.api_key import ApiKey
from orchestration.storage.models.project import Project
from orchestration.storage.models.suggestion import Suggestion
from orchestration.storage.models.task import Task
from orchestration.storage.models.user import User

# --- fixtures ---------------------------------------------------------------


async def _make_user_and_key(email: str = "picker@example.com") -> str:
    """Insert a User + ApiKey, return the raw key the test can use."""
    factory = get_session_factory()
    raw = f"picker-key-{uuid.uuid4()}"
    async with factory() as session:
        user_id = (
            await session.execute(
                insert(User)
                .values(email=email, display_name="Picker", role="admin")
                .returning(User.id)
            )
        ).scalar_one()
        await session.execute(
            insert(ApiKey).values(user_id=user_id, key_hash=_hash_key(raw), name="picker-test")
        )
        await session.commit()
    return raw


async def _make_project() -> uuid.UUID:
    factory = get_session_factory()
    pid = uuid.uuid4()
    async with factory() as session:
        session.add(Project(id=pid, name="proj", repo_url="https://example.com/repo.git"))
        await session.commit()
    return pid


def _proposals() -> list[dict[str, Any]]:
    return [
        {
            "title": "Add metrics endpoint",
            "description": "Expose Prometheus metrics.",
            "rationale": "Ops needs visibility into worker health.",
            "estimated_complexity": "small",
            "related_files": ["src/api/metrics.py"],
        },
        {
            "title": "Migrate storage to v2",
            "description": "Switch JSONB layout to the new schema.",
            "rationale": "Old schema is at EOL.",
            "estimated_complexity": "large",
            "related_files": ["src/storage/models/*.py"],
        },
        {
            "title": "Document orchestrator flow",
            "description": "Add an architecture diagram and writeup.",
            "rationale": "Onboarding is rough without docs.",
            "estimated_complexity": "medium",
            "related_files": ["docs/orchestrator.md"],
        },
    ]


async def _make_suggestion(
    pid: uuid.UUID,
    *,
    status: str = "pending",
    proposals: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    factory = get_session_factory()
    sid = uuid.uuid4()
    async with factory() as session:
        session.add(
            Suggestion(
                id=sid,
                project_id=pid,
                status=status,
                proposals=proposals if proposals is not None else _proposals(),
            )
        )
        await session.commit()
    return sid


async def _auth_headers() -> dict[str, str]:
    raw = await _make_user_and_key()
    return {"X-API-Key": raw}


# --- auth -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_suggestions_requires_auth(client):
    pid = uuid.uuid4()
    r = await client.get(
        f"/projects/{pid}/suggestions", headers={"X-API-Key": "definitely-not-valid"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_suggestion_requires_auth(client):
    sid = uuid.uuid4()
    r = await client.get(
        f"/projects/{uuid.uuid4()}/suggestions/{sid}",
        headers={"X-API-Key": "definitely-not-valid"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_pick_suggestion_requires_auth(client):
    sid = uuid.uuid4()
    r = await client.post(
        f"/projects/{uuid.uuid4()}/suggestions/{sid}/pick",
        headers={"X-API-Key": "definitely-not-valid"},
        json={"picks": [{"index": 0}]},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_rejects_bad_api_key(client):
    pid = uuid.uuid4()
    r = await client.get(
        f"/projects/{pid}/suggestions", headers={"X-API-Key": "definitely-not-valid"}
    )
    assert r.status_code == 401


# --- list -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_pending_suggestions_only(client):
    headers = await _auth_headers()
    pid = await _make_project()
    pending_a = await _make_suggestion(pid, status="pending")
    pending_b = await _make_suggestion(pid, status="pending")
    await _make_suggestion(pid, status="picked")

    r = await client.get(f"/projects/{pid}/suggestions", headers=headers)
    assert r.status_code == 200
    body = r.json()
    ids = {s["id"] for s in body["suggestions"]}
    assert ids == {str(pending_a), str(pending_b)}
    for item in body["suggestions"]:
        assert set(item.keys()) == {"id", "status", "created_at", "proposal_count"}
        assert item["status"] == "pending"
        assert item["proposal_count"] == 3


@pytest.mark.asyncio
async def test_list_unknown_project_returns_404(client):
    headers = await _auth_headers()
    r = await client.get(f"/projects/{uuid.uuid4()}/suggestions", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_empty_when_none_pending(client):
    headers = await _auth_headers()
    pid = await _make_project()
    r = await client.get(f"/projects/{pid}/suggestions", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"suggestions": []}


# --- get one ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_full_proposals(client):
    headers = await _auth_headers()
    pid = await _make_project()
    sid = await _make_suggestion(pid)

    r = await client.get(f"/projects/{pid}/suggestions/{sid}", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(sid)
    assert body["project_id"] == str(pid)
    assert body["status"] == "pending"
    assert len(body["proposals"]) == 3
    assert body["proposals"][0]["title"] == "Add metrics endpoint"


@pytest.mark.asyncio
async def test_get_wrong_project_returns_404(client):
    headers = await _auth_headers()
    pid_real = await _make_project()
    sid = await _make_suggestion(pid_real)
    other_pid = await _make_project()

    r = await client.get(f"/projects/{other_pid}/suggestions/{sid}", headers=headers)
    assert r.status_code == 404


# --- pick -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pick_creates_tasks_and_marks_suggestion_picked(client):
    headers = await _auth_headers()
    pid = await _make_project()
    sid = await _make_suggestion(pid)

    r = await client.post(
        f"/projects/{pid}/suggestions/{sid}/pick",
        headers=headers,
        json={"picks": [{"index": 0}, {"index": 2}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["picked_count"] == 2
    assert body["auto_execute"] is False
    assert len(body["task_ids"]) == 2

    # Suggestion is now picked with a decided_at.
    factory = get_session_factory()
    async with factory() as session:
        s = await session.get(Suggestion, sid)
        assert s is not None
        assert s.status == "picked"
        assert s.decided_at is not None

        tasks = (
            await session.execute(select(Task).where(Task.project_id == pid))
        ).scalars().all()
        assert len(tasks) == 2
        by_idx = {t.task_metadata["proposal_index"]: t for t in tasks}
        assert set(by_idx.keys()) == {0, 2}

        t0 = by_idx[0]
        assert t0.title == "Add metrics endpoint"
        assert t0.source_type == "csv"
        assert t0.project_id == pid
        assert t0.external_id == f"suggestion:{sid}:0"
        assert t0.status == "pending"
        assert t0.task_metadata["source"] == "suggestion"
        assert t0.task_metadata["estimated_complexity"] == "small"

        t2 = by_idx[2]
        assert t2.title == "Document orchestrator flow"


@pytest.mark.asyncio
async def test_pick_deduplicates_repeated_indices(client):
    headers = await _auth_headers()
    pid = await _make_project()
    sid = await _make_suggestion(pid)

    r = await client.post(
        f"/projects/{pid}/suggestions/{sid}/pick",
        headers=headers,
        json={"picks": [{"index": 1}, {"index": 1}, {"index": 1}]},
    )
    assert r.status_code == 200
    assert r.json()["picked_count"] == 1

    factory = get_session_factory()
    async with factory() as session:
        n = (
            await session.execute(select(Task).where(Task.project_id == pid))
        ).scalars().all()
    assert len(n) == 1


@pytest.mark.asyncio
async def test_pick_rejects_out_of_range_index(client):
    headers = await _auth_headers()
    pid = await _make_project()
    sid = await _make_suggestion(pid)

    r = await client.post(
        f"/projects/{pid}/suggestions/{sid}/pick",
        headers=headers,
        json={"picks": [{"index": 99}]},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_pick_rejects_already_picked(client):
    headers = await _auth_headers()
    pid = await _make_project()
    sid = await _make_suggestion(pid)

    r1 = await client.post(
        f"/projects/{pid}/suggestions/{sid}/pick",
        headers=headers,
        json={"picks": [{"index": 0}]},
    )
    assert r1.status_code == 200

    r2 = await client.post(
        f"/projects/{pid}/suggestions/{sid}/pick",
        headers=headers,
        json={"picks": [{"index": 1}]},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_pick_wrong_project_returns_404(client):
    headers = await _auth_headers()
    pid_real = await _make_project()
    sid = await _make_suggestion(pid_real)
    other_pid = await _make_project()

    r = await client.post(
        f"/projects/{other_pid}/suggestions/{sid}/pick",
        headers=headers,
        json={"picks": [{"index": 0}]},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_pick_auto_execute_is_accepted(client, caplog):
    headers = await _auth_headers()
    pid = await _make_project()
    sid = await _make_suggestion(pid)

    with caplog.at_level("INFO", logger="orchestration.api.routes.suggestions"):
        r = await client.post(
            f"/projects/{pid}/suggestions/{sid}/pick",
            headers=headers,
            json={"picks": [{"index": 0}], "auto_execute": True},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["auto_execute"] is True
    assert body["picked_count"] == 1

    # The route should have logged its intent to execute.
    assert any("auto_execute requested" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_pick_requires_at_least_one_pick(client):
    headers = await _auth_headers()
    pid = await _make_project()
    sid = await _make_suggestion(pid)

    r = await client.post(
        f"/projects/{pid}/suggestions/{sid}/pick",
        headers=headers,
        json={"picks": []},
    )
    assert r.status_code == 422  # pydantic validation: min_length=1

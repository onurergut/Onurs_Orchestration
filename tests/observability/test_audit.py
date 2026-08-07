"""Tests for the audit log middleware (M3/Task 27).

``stage_audit`` is a thin async context manager that emits
``stage:{name}/enter`` on entry and ``stage:{name}/exit`` or
``stage:{name}/error`` on exit. Every test seeds a Project + Task + Run
so the audit_log table's FKs hold, then reads the rows back via the
session_scope to assert shape and ordering.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from orchestration.observability.audit_middleware import stage_audit
from orchestration.storage.db import session_scope
from orchestration.storage.models.audit_log import AuditLog
from orchestration.storage.models.project import Project
from orchestration.storage.models.run import Run
from orchestration.storage.models.task import Task


async def _seed_run() -> tuple[uuid.UUID, uuid.UUID]:
    """Seed a Project + Task + Run so audit_log FKs are satisfied.

    Returns ``(project_id, run_id)`` — the IDs the callers will pass to
    ``stage_audit``. The Task is required because the Run row carries a
    non-null ``task_id`` FK.
    """
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with session_scope() as session:
        session.add(Project(id=project_id, name="t", repo_url="https://example.com/repo"))
        await session.flush()
        session.add(Task(id=task_id, project_id=project_id, external_id="t-1", source_type="csv", title="t"))
        await session.flush()
        session.add(
            Run(
                id=run_id,
                project_id=project_id,
                task_id=task_id,
                flow_uuid=uuid.uuid4(),
            )
        )
    return project_id, run_id


async def _fetch_audit_rows(run_id: uuid.UUID) -> list[AuditLog]:
    async with session_scope() as session:
        rows = (await session.execute(
            select(AuditLog).where(AuditLog.run_id == run_id).order_by(AuditLog.ts)
        )).scalars().all()
        return list(rows)


@pytest.mark.asyncio
async def test_stage_audit_emits_enter_and_exit_on_success() -> None:
    """Success path: ``enter`` is emitted on entry, ``exit`` on exit.

    The exit payload carries ``status=success`` and a populated
    ``duration_ms``. We assert the exact audit shape so a future
    refactor (e.g. renaming the action or dropping the duration field)
    fails loudly here rather than silently at the analytics layer.
    """
    project_id, run_id = await _seed_run()

    async with stage_audit(project_id, run_id, "planner") as outcome:
        # Yielded dict is the same object as the inner outcome; the
        # caller can inspect status/duration_ms after the block.
        assert outcome["status"] == "success"
        # Body runs here.
        await asyncio.sleep(0.01)

    rows = await _fetch_audit_rows(run_id)
    assert len(rows) == 2

    # First row: enter.
    assert rows[0].actor == "stage:planner"
    assert rows[0].action == "enter"
    assert rows[0].payload == {}

    # Second row: exit with status=success + duration_ms.
    assert rows[1].actor == "stage:planner"
    assert rows[1].action == "exit"
    assert rows[1].payload["status"] == "success"
    assert isinstance(rows[1].payload["duration_ms"], int)
    assert rows[1].payload["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_stage_audit_emits_error_on_exception() -> None:
    """Error path: ``enter`` is emitted on entry, ``error`` on exit.

    The exception's class name and a truncated message are recorded
    in ``payload.error``, and the original exception is re-raised so
    the flow's normal error path is preserved.
    """
    project_id, run_id = await _seed_run()

    class _BoomError(Exception):
        pass

    with pytest.raises(_BoomError):
        async with stage_audit(project_id, run_id, "coder"):
            await asyncio.sleep(0.005)
            raise _BoomError("kaboom")

    rows = await _fetch_audit_rows(run_id)
    assert len(rows) == 2

    # First row: enter.
    assert rows[0].action == "enter"
    assert rows[0].payload == {}

    # Second row: error with status=failure + truncated error string.
    assert rows[1].actor == "stage:coder"
    assert rows[1].action == "error"
    assert rows[1].payload["status"] == "failure"
    assert rows[1].payload["error"].startswith("_BoomError: kaboom")
    assert isinstance(rows[1].payload["duration_ms"], int)
    assert rows[1].payload["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_stage_audit_truncates_long_error_messages() -> None:
    """The audit_log payload keeps error strings <=200 chars to avoid
    bloating the JSONB column. The class-name prefix is preserved; only
    the message portion is truncated.
    """
    project_id, run_id = await _seed_run()
    long_msg = "x" * 500

    with pytest.raises(RuntimeError):
        async with stage_audit(project_id, run_id, "tester"):
            raise RuntimeError(long_msg)

    rows = await _fetch_audit_rows(run_id)
    assert len(rows) == 2
    error = rows[1].payload["error"]
    assert error.startswith("RuntimeError: ")
    # The full payload string is "RuntimeError: " (15 chars) + 200 chars of the message.
    assert len(error) == len("RuntimeError: ") + 200


@pytest.mark.asyncio
async def test_stage_audit_yields_outcome_dict_with_status() -> None:
    """The yielded dict is the inner outcome — the caller can read
    ``status`` mid-flight and the value after the block reflects the
    final state (success or failure).
    """
    project_id, run_id = await _seed_run()

    async with stage_audit(project_id, run_id, "reviewer") as outcome:
        assert outcome["status"] == "success"
        assert "duration_ms" not in outcome  # populated in finally, after yield

    assert outcome["status"] == "success"
    assert isinstance(outcome["duration_ms"], int)

import uuid

import pytest

from orchestration.storage.db import session_scope
from orchestration.storage.models.external_receipt import ExternalReceipt
from orchestration.storage.models.project import Project
from orchestration.storage.models.run import Run
from orchestration.storage.models.task import Task
from orchestration.storage.repositories.receipts import with_receipt
from sqlalchemy import select


async def _seed_run() -> uuid.UUID:
    """Seed a Project + Task + Run so external_receipts.run_id FK is satisfied."""
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
    return run_id


@pytest.mark.asyncio
async def test_receipt_first_call_executes_and_caches():
    run_id = await _seed_run()
    key = f"test:{run_id}:create"
    async with session_scope() as session:
        async with with_receipt(
            session,
            run_id=run_id,
            idempotency_key=key,
            adapter_type="test",
            operation="create",
            request_payload={"x": 1},
        ) as ctx:
            assert ctx.cached is False
            ctx.response = {"id": "created-1"}

    async with session_scope() as session:
        rows = (await session.execute(
            select(ExternalReceipt).where(ExternalReceipt.idempotency_key == key)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "succeeded"
        assert rows[0].response_payload == {"id": "created-1"}


@pytest.mark.asyncio
async def test_receipt_second_call_returns_cached():
    run_id = await _seed_run()
    key = f"test:{run_id}:dedup"
    async with session_scope() as session:
        async with with_receipt(
            session, run_id=run_id, idempotency_key=key,
            adapter_type="test", operation="create", request_payload={"x": 1},
        ) as ctx:
            ctx.response = {"id": "first"}

    async with session_scope() as session:
        async with with_receipt(
            session, run_id=run_id, idempotency_key=key,
            adapter_type="test", operation="create", request_payload={"x": 1},
        ) as ctx:
            assert ctx.cached is True
            assert ctx.response == {"id": "first"}

    async with session_scope() as session:
        rows = (await session.execute(
            select(ExternalReceipt).where(ExternalReceipt.idempotency_key == key)
        )).scalars().all()
        assert len(rows) == 1  # dedup: still only one receipt

import asyncio
import json
from uuid import uuid4

import pytest

from orchestration.adapters.review.base import Artifacts
from orchestration.adapters.review.web_ui import WebUiAdapter
from orchestration.storage.db import session_scope
from orchestration.storage.models import Project, Run, Task
from orchestration.storage.redis import get_redis


async def _seed_run_chain() -> tuple[uuid4, uuid4]:
    """Insert a minimal Project + Task + Run so ``notify_review`` can resolve
    ``project_id`` from the Run row via ``WebUiAdapter._resolve_project_id``.

    The autouse ``_isolate_state`` fixture (from ``tests/conftest.py``)
    TRUNCATEs every table before each test, so the IDs never collide.
    Flushes parents before adding the child so SQLAlchemy doesn't try to
    INSERT the Run before its Task/Project exist — the dependency-based
    ``sort_tables`` walker doesn't always get the order right when three
    FK-linked rows are added in a single session.
    """
    project_id = uuid4()
    task_id = uuid4()
    run_id = uuid4()
    async with session_scope() as session:
        project = Project(
            id=project_id,
            name="webui-test-project",
            repo_url="https://example.com/webui-test.git",
            default_branch="main",
        )
        session.add(project)
        await session.flush()

        task = Task(
            id=task_id,
            project_id=project.id,
            external_id="webui-T1",
            source_type="csv",
            title="Web UI test task",
            description=None,
            status="pending",
        )
        session.add(task)
        await session.flush()

        run = Run(
            id=run_id,
            project_id=project.id,
            task_id=task.id,
            flow_uuid=uuid4(),
            status="running",
            current_stage="plan",
        )
        session.add(run)
        await session.flush()
    return run_id, project_id


@pytest.mark.asyncio
async def test_web_ui_decision_round_trip():
    adapter = WebUiAdapter()
    run_id, _ = await _seed_run_chain()
    receipt = await adapter.notify_review(run_id, Artifacts(summary="x"))

    async def publish():
        await asyncio.sleep(0.05)
        redis = get_redis()
        await redis.publish(f"review:decision:{receipt.id}", json.dumps({"approve": True, "comment": "lgtm"}))

    asyncio.create_task(publish())
    decision = await adapter.await_decision(receipt, timeout_s=5)
    assert decision.approve
    assert decision.comment == "lgtm"

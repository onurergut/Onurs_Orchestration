import uuid

import pytest

from orchestration.orchestrator.budget_guard import BudgetExceeded, check
from orchestration.storage.db import session_scope
from orchestration.storage.models.budget_state import BudgetState
from orchestration.storage.models.project import Project
from orchestration.storage.repositories import budget as budget_repo


@pytest.mark.asyncio
async def test_budget_guard_raises_when_exceeded():
    pid = uuid.uuid4()
    async with session_scope() as session:
        session.add(Project(id=pid, name="t", repo_url="x", budget_config={"monthly_budget_usd": 10.0}))
        await session.commit()

    try:
        await budget_repo.record_spend(pid, 5.0, None)
        await budget_repo.record_spend(pid, 6.0, None)  # 11 > 10

        with pytest.raises(BudgetExceeded):
            await check(pid)
    finally:
        # Cleanup so test is idempotent across reruns
        async with session_scope() as session:
            await session.execute(
                BudgetState.__table__.delete().where(BudgetState.project_id == pid)
            )


@pytest.mark.asyncio
async def test_budget_guard_passes_under_cap():
    pid = uuid.uuid4()
    async with session_scope() as session:
        session.add(Project(id=pid, name="t", repo_url="x", budget_config={"monthly_budget_usd": 100.0}))
        await session.commit()

    try:
        await budget_repo.record_spend(pid, 25.0, None)
        await check(pid)  # should not raise
    finally:
        async with session_scope() as session:
            await session.execute(
                BudgetState.__table__.delete().where(BudgetState.project_id == pid)
            )

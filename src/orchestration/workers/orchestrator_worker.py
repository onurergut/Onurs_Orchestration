import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update

from orchestration.orchestrator.flow import OrchestratorFlow
from orchestration.orchestrator.state import OrchestratorState
from orchestration.storage.db import session_scope
from orchestration.storage.models.run import Run


async def run_flow(run_id: UUID) -> None:
    async with session_scope() as session:
        run = (await session.execute(
            select(Run).where(Run.id == run_id)
        )).scalar_one()
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status="running", started_at=datetime.now(timezone.utc))
        )

    state = OrchestratorState(
        run_id=run_id,
        project_id=run.project_id,
        task_id=run.task_id,
    )
    flow = OrchestratorFlow()
    flow.state = state
    await flow.kickoff_async() if hasattr(flow, "kickoff_async") else flow.kickoff()


if __name__ == "__main__":
    import sys
    asyncio.run(run_flow(UUID(sys.argv[1])))
import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update

from orchestration.observability.metrics import (
    dec_runs_in_progress,
    inc_runs_failed,
    inc_runs_in_progress,
)
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
            .values(status="running", started_at=datetime.now(UTC))
        )

    state = OrchestratorState(
        run_id=run_id,
        project_id=run.project_id,
        task_id=run.task_id,
    )
    flow = OrchestratorFlow(initial_state=state)

    # Bump the in-progress gauge for the duration of the run. The
    # ``finally`` block always decrements it so a raise path doesn't
    # leak the gauge above zero forever.
    inc_runs_in_progress()
    terminal_status = "succeeded"
    try:
        if hasattr(flow, "kickoff_async"):
            await flow.kickoff_async()
        else:
            flow.kickoff()
    except Exception:
        terminal_status = "failed"
        inc_runs_failed()
        raise
    finally:
        dec_runs_in_progress()
        async with session_scope() as session:
            await session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(status=terminal_status, finished_at=datetime.now(UTC))
            )


if __name__ == "__main__":
    import sys
    asyncio.run(run_flow(UUID(sys.argv[1])))
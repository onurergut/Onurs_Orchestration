from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orchestration.storage.db import session_scope
from orchestration.storage.models.budget_state import BudgetState
from orchestration.storage.models.project import Project


def month_start(d: date | None = None) -> date:
    d = d or date.today()
    return d.replace(day=1)


async def current_spend(project_id: UUID) -> float:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(BudgetState).where(
                    BudgetState.project_id == project_id,
                    BudgetState.month == month_start(),
                )
            )
        ).scalar_one_or_none()
        return float(row.spent_usd) if row else 0.0


async def record_spend(project_id: UUID, amount_usd: float, run_id: UUID | None) -> None:
    async with session_scope() as session:
        stmt = pg_insert(BudgetState).values(
            project_id=project_id,
            month=month_start(),
            spent_usd=amount_usd,
            last_run_id=run_id,
        ).on_conflict_do_update(
            index_elements=["project_id", "month"],
            set_={"spent_usd": BudgetState.spent_usd + amount_usd, "last_run_id": run_id},
        )
        await session.execute(stmt)


async def project_budget(project_id: UUID) -> float:
    async with session_scope() as session:
        proj = (await session.execute(select(Project).where(Project.id == project_id))).scalar_one()
        return float(proj.budget_config.get("monthly_budget_usd", 500.0))

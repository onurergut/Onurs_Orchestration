from uuid import UUID

from orchestration.storage.repositories.budget import current_spend, project_budget


class BudgetExceeded(Exception):
    pass


async def check(project_id: UUID) -> None:
    spent = await current_spend(project_id)
    cap = await project_budget(project_id)
    if spent >= cap:
        raise BudgetExceeded(f"project {project_id} budget exhausted: ${spent:.2f} / ${cap:.2f}")

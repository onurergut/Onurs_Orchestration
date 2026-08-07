from uuid import UUID

from sqlalchemy import insert

from orchestration.storage.db import session_scope
from orchestration.storage.models.audit_log import AuditLog


async def audit(project_id: UUID, run_id: UUID | None, actor: str, action: str, payload: dict) -> None:
    async with session_scope() as session:
        await session.execute(
            insert(AuditLog).values(
                run_id=run_id,
                project_id=project_id,
                actor=actor,
                action=action,
                payload=payload,
            )
        )

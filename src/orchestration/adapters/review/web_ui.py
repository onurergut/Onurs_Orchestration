import asyncio
import json
from uuid import UUID, uuid4

from sqlalchemy import insert, select

from orchestration.adapters.review.base import Artifacts, Decision, Receipt, ReviewSurfaceAdapter
from orchestration.storage.db import session_scope
from orchestration.storage.models.audit_log import AuditLog
from orchestration.storage.models.external_receipt import ExternalReceipt
from orchestration.storage.models.run import Run
from orchestration.storage.redis import get_redis


class WebUiAdapter(ReviewSurfaceAdapter):
    name = "web_ui"

    async def _resolve_project_id(self, session, run_id: UUID) -> UUID:
        run = (
            await session.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"run {run_id} not found")
        return run.project_id

    async def notify_review(self, run_id: UUID, artifacts: Artifacts) -> Receipt:
        receipt_id = uuid4()
        async with session_scope() as session:
            project_id = await self._resolve_project_id(session, run_id)
            await session.execute(
                insert(ExternalReceipt).values(
                    id=receipt_id,
                    run_id=run_id,
                    idempotency_key=f"web_ui:notify:{run_id}",
                    adapter_type="web_ui",
                    operation="notify",
                    request_payload=artifacts.model_dump(),
                    status="succeeded",
                )
            )
            await session.execute(
                insert(AuditLog).values(
                    run_id=run_id,
                    project_id=project_id,
                    actor="system:review",
                    action="notify_review",
                    payload=artifacts.model_dump(),
                )
            )
        return Receipt(id=receipt_id, external_id=str(receipt_id))

    async def await_decision(self, receipt: Receipt, timeout_s: int) -> Decision:
        redis = get_redis()
        channel = f"review:decision:{receipt.id}"
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async with asyncio.timeout(timeout_s):
                async for msg in pubsub.listen():
                    if msg["type"] == "message":
                        return Decision(**json.loads(msg["data"]))
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
        raise TimeoutError("no decision received")

    async def post_comment(self, run_id: UUID, body: str) -> None:
        async with session_scope() as session:
            project_id = await self._resolve_project_id(session, run_id)
            await session.execute(
                insert(AuditLog).values(
                    run_id=run_id,
                    project_id=project_id,
                    actor="system:review",
                    action="post_comment",
                    payload={"body": body},
                )
            )

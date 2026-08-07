from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestration.storage.models.external_receipt import ExternalReceipt


@dataclass
class ReceiptContext:
    """Handle yielded by with_receipt(). Caller sets `.response` to cache the side-effect result.

    On a cache hit, `.cached` is True and the original `.response` is already populated;
    caller should NOT re-execute the side-effect.
    """

    cached: bool = False
    response: Any = None
    _on_complete: Any = None  # callable(response) -> awaitable, set by helper


@asynccontextmanager
async def with_receipt(
    session: AsyncSession,
    *,
    run_id: UUID,
    idempotency_key: str,
    adapter_type: str,
    operation: str,
    request_payload: dict,
):
    """Look up an existing receipt; on hit yield cached response, on miss yield fresh ctx.

    Usage:
        async with with_receipt(session, ...) as ctx:
            if ctx.cached:
                return  # use ctx.response
            # ... do side-effect ...
            ctx.response = {"id": "...", ...}
    """
    existing = (
        await session.execute(
            select(ExternalReceipt).where(ExternalReceipt.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()

    if existing and existing.status == "succeeded":
        ctx = ReceiptContext(cached=True, response=existing.response_payload)
        yield ctx
        return

    receipt_id = existing.id if existing else uuid4()
    if not existing:
        await session.execute(
            insert(ExternalReceipt).values(
                id=receipt_id,
                run_id=run_id,
                idempotency_key=idempotency_key,
                adapter_type=adapter_type,
                operation=operation,
                request_payload=request_payload,
                status="pending",
            )
        )

    ctx = ReceiptContext(cached=False, response=None)
    try:
        yield ctx
    except Exception:
        await session.execute(
            update(ExternalReceipt)
            .where(ExternalReceipt.id == receipt_id)
            .values(status="failed")
        )
        raise
    else:
        await session.execute(
            update(ExternalReceipt)
            .where(ExternalReceipt.id == receipt_id)
            .values(status="succeeded", response_payload=ctx.response)
        )

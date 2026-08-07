from fastapi import APIRouter
from sqlalchemy import text

from orchestration.storage.db import get_session_factory
from orchestration.storage.redis import get_redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, str]:
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(text("SELECT 1"))
    redis = get_redis()
    await redis.ping()
    return {"status": "ready"}

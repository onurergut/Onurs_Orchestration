from fastapi import Depends, Header, HTTPException, status
from passlib.hash import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestration.storage.db import get_session_factory
from orchestration.storage.models.api_key import ApiKey
from orchestration.storage.models.user import User


async def get_session() -> AsyncSession:  # type: ignore[misc]  # async generator yield, FastAPI dependency
    factory = get_session_factory()
    async with factory() as session:
        yield session


def _hash_key(raw: str) -> str:
    return bcrypt.using(rounds=4).hash(raw)  # rounds=4 for test speed


def verify_key(raw: str, hashed: str) -> bool:
    return bcrypt.verify(raw, hashed)


async def require_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    session: AsyncSession = Depends(get_session),
) -> User:
    stmt = select(ApiKey, User).join(User, User.id == ApiKey.user_id).where(ApiKey.revoked_at.is_(None))
    result = await session.execute(stmt)
    rows = result.all()
    for api_key, user in rows:
        if verify_key(x_api_key, api_key.key_hash):
            return user
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")

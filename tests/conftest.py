import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")

from passlib.hash import bcrypt as _bcrypt  # noqa: E402


def _hash_key(raw: str) -> str:
    return _bcrypt.using(rounds=4).hash(raw)


from orchestration.api.app import create_app  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _isolate_state() -> AsyncIterator[None]:
    # Each pytest-asyncio test gets its own event loop, but the lazy async engine
    # singleton in orchestration.storage.db is loop-bound. Also, the test DB
    # persists rows across tests. This fixture ensures every test starts on a
    # fresh engine over an empty schema.
    from sqlalchemy import text

    from orchestration.storage import db as _db
    from orchestration.storage.models import Base

    factory = _db.get_session_factory()
    async with factory() as session:
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await session.commit()

    yield

    engine = _db._engine
    if engine is not None:
        await engine.dispose()
        _db._engine = None
        _db._session_factory = None

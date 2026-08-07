import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://orchestration:orchestration@localhost:5432/orchestration_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")

from orchestration.api.app import create_app  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

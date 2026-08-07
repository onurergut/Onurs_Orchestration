import pytest
from sqlalchemy import insert
from tests.conftest import _hash_key  # re-export helper

from orchestration.storage.db import get_session_factory
from orchestration.storage.models.api_key import ApiKey
from orchestration.storage.models.user import User


@pytest.mark.asyncio
async def test_me_requires_api_key(client):
    response = await client.get("/me")
    assert response.status_code == 422  # missing header


@pytest.mark.asyncio
async def test_me_rejects_bad_key(client):
    response = await client.get("/me", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_accepts_valid_key(client):
    factory = get_session_factory()
    raw_key = "test-key-abc123"
    async with factory() as session:
        user = (await session.execute(
            insert(User).values(email="t@example.com", display_name="T", role="admin").returning(User.id)
        )).scalar_one()
        await session.execute(
            insert(ApiKey).values(user_id=user, key_hash=_hash_key(raw_key), name="test")
        )
        await session.commit()

    response = await client.get("/me", headers={"X-API-Key": raw_key})
    assert response.status_code == 200
    assert response.json()["email"] == "t@example.com"

import pytest

from orchestration.api.app import create_app


@pytest.mark.asyncio
async def test_full_stack_boots():
    app = create_app()
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        h = await client.get("/health")
        assert h.status_code == 200
        assert h.json()["status"] == "ok"

        r = await client.get("/ready")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"

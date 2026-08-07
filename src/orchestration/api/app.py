from fastapi import Depends, FastAPI

from orchestration.api.middleware.api_key import require_api_key
from orchestration.api.routes.health import router as health_router
from orchestration.storage.models.user import User


def create_app() -> FastAPI:
    app = FastAPI(title="Orchestration", version="0.1.0")
    app.include_router(health_router)

    @app.get("/me", tags=["auth"])
    async def me(user: User = Depends(require_api_key)) -> dict[str, str]:
        return {"email": user.email, "role": user.role}

    return app

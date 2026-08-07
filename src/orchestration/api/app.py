from fastapi import Depends, FastAPI
from fastapi.responses import Response

from orchestration.api.middleware.api_key import require_api_key
from orchestration.api.routes.health import router as health_router
from orchestration.api.routes.suggestions import router as suggestions_router
from orchestration.observability.metrics import render_metrics
from orchestration.storage.models.user import User


def create_app() -> FastAPI:
    app = FastAPI(title="Orchestration", version="0.1.0")
    app.include_router(health_router)
    app.include_router(suggestions_router)

    @app.get("/me", tags=["auth"])
    async def me(user: User = Depends(require_api_key)) -> dict[str, str]:
        return {"email": user.email, "role": user.role}

    # Prometheus scrape endpoint. Intentionally unauthenticated and
    # excluded from the OpenAPI schema: scrapers don't present API keys
    # and the schema is for human consumption, not telemetry.
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        body, content_type = render_metrics()
        return Response(content=body, media_type=content_type)

    return app

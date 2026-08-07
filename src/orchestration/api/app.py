from fastapi import FastAPI

from orchestration.api.routes.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(title="Orchestration", version="0.1.0")
    app.include_router(health_router)
    return app

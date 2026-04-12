from fastapi import FastAPI

from src.api.agents import router as agents_router
from src.api.errors import register_exception_handlers
from src.api.services import ServiceContainer, build_default_services


def create_app(*, services: ServiceContainer | None = None) -> FastAPI:
    app = FastAPI()
    app.state.services = services or build_default_services()
    register_exception_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(agents_router)

    return app

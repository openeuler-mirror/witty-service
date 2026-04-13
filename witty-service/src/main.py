from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.agents import router as agents_router
from src.api.errors import register_exception_handlers
from src.api.services import ServiceContainer, build_default_services
from src.config import get_settings


def create_app(*, services: ServiceContainer | None = None) -> FastAPI:
    app = FastAPI()
    app.state.services = services or build_default_services()
    register_exception_handlers(app)

    # 从配置获取 CORS 设置
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_credentials,
        allow_methods=settings.cors_methods,
        allow_headers=settings.cors_headers,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(agents_router)

    return app

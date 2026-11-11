from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from witty_service.api.contract.routers import router as contract_router
from witty_service.api.cve import router as cve_router
from witty_service.api.errors import register_exception_handlers
from witty_service.api.openapi import ApiApp
from witty_service.api.services import ServiceContainer, build_default_services
from witty_service.config import SUPPORTED_RUNTIME_TYPES, get_settings
from witty_service.logger import configure_logging
from witty_service.persistence.db import checkpoint_database

logger = logging.getLogger(__name__)

_CHECKPOINT_INTERVAL_S = 3600.0


def create_app(services: ServiceContainer | None = None) -> FastAPI:
    configure_logging()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop_event = threading.Event()

        def checkpoint_forever() -> None:
            while not stop_event.wait(_CHECKPOINT_INTERVAL_S):
                try:
                    checkpoint_database()
                except Exception:
                    logger.exception("Periodic wal_checkpoint failed")

        threading.Thread(
            target=checkpoint_forever, name="wal-checkpoint", daemon=True
        ).start()
        try:
            yield
        finally:
            stop_event.set()
            await app.state.services.close()

    app = ApiApp(title="Witty Service", lifespan=lifespan)
    app.state.services = services or build_default_services()
    register_exception_handlers(app)

    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.origins,
        allow_credentials=settings.cors.credentials,
        allow_methods=settings.cors.methods,
        allow_headers=settings.cors.headers,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/server/capabilities")
    def capabilities() -> dict[str, list[str]]:
        return {"supported_runtimes": list(SUPPORTED_RUNTIME_TYPES)}

    app.include_router(cve_router)
    app.include_router(contract_router)
    return app

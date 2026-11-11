from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from witty_service.application.model_catalog_service import ModelCatalogService
from witty_service.bootstrap import build_workspace_runtime_manager
from witty_service.config import get_settings
from witty_service.persistence.consistency import verify_structure
from witty_service.persistence.db import (
    create_session_factory,
    create_sqlite_engine,
    init_db,
)
from witty_service.persistence.repository import (
    AgentRepository,
    CatalogRepository,
    SessionRepository,
    WorkspaceRepository,
)
from witty_service.persistence.seed import seed_defaults
from witty_service.runtime.manager import WorkspaceRuntimeManager
from witty_service.sandbox.base import SandboxBackend
from witty_service.sandbox.factory import create_sandbox_backend
from witty_service.storage.credentials import ModelCredentialStore
from witty_service.storage.workspace_store import LocalWorkspaceStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ServiceContainer:
    engine: Engine
    session_factory: sessionmaker[Session]
    catalog_repository: CatalogRepository
    workspace_store: LocalWorkspaceStore
    agents: AgentRepository
    workspaces: WorkspaceRepository
    sessions: SessionRepository
    runtime_manager: WorkspaceRuntimeManager
    model_catalog: ModelCatalogService
    sandbox_backends: dict[str, SandboxBackend] = field(default_factory=dict)

    def get_sandbox_backend(self, sandbox_type: str) -> SandboxBackend:
        backend = self.sandbox_backends.get(sandbox_type)
        if backend is None:
            backend = create_sandbox_backend(sandbox_type)
            self.sandbox_backends[sandbox_type] = backend
        return backend

    def _stop_sandbox_backends(self) -> None:
        for backend in self.sandbox_backends.values():
            try:
                backend.stop_all()
            except Exception:
                logger.exception("Failed to stop sandbox backend")

    async def close(self) -> None:
        try:
            await self.runtime_manager.shutdown()
        finally:
            try:
                if get_settings().workspace.stop_sandboxes_on_shutdown:
                    self._stop_sandbox_backends()
            finally:
                self.engine.dispose()


def _ensure_database_directory(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    path = Path(database_url.removeprefix("sqlite:///")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)


def build_default_services() -> ServiceContainer:
    settings = get_settings()
    database_url = settings.database.url
    _ensure_database_directory(database_url)
    engine = create_sqlite_engine(database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    workspace_root = Path(settings.workspace.root).expanduser()
    seed_defaults(
        session_factory, host_directory=workspace_root / "workspaces" / "default"
    )
    verify_structure(engine)
    sandbox = create_sandbox_backend("local_process")
    catalog_repository = CatalogRepository(session_factory)
    return ServiceContainer(
        engine=engine,
        session_factory=session_factory,
        catalog_repository=catalog_repository,
        workspace_store=LocalWorkspaceStore(workspace_root),
        agents=AgentRepository(session_factory),
        workspaces=WorkspaceRepository(session_factory),
        sessions=SessionRepository(session_factory),
        runtime_manager=build_workspace_runtime_manager(sandbox=sandbox),
        model_catalog=ModelCatalogService(
            catalog_repository,
            ModelCredentialStore(settings.model.credentials_dir),
        ),
        sandbox_backends={"local_process": sandbox},
    )

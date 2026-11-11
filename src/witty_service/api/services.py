from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from witty_service.config import get_settings
from witty_service.persistence.consistency import verify_structure
from witty_service.persistence.db import (
    create_session_factory,
    create_sqlite_engine,
    init_db,
)
from witty_service.persistence.repository import CatalogRepository
from witty_service.persistence.seed import seed_defaults
from witty_service.storage.workspace_store import LocalWorkspaceStore


@dataclass(slots=True)
class ServiceContainer:
    engine: Engine
    session_factory: sessionmaker[Session]
    catalog_repository: CatalogRepository
    workspace_store: LocalWorkspaceStore

    async def close(self) -> None:
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
    return ServiceContainer(
        engine=engine,
        session_factory=session_factory,
        catalog_repository=CatalogRepository(session_factory),
        workspace_store=LocalWorkspaceStore(workspace_root),
    )

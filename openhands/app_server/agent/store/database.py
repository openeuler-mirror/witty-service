"""Database configuration for agent store."""

import os
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession


def get_agentd_workspace_path() -> Path:
    return Path(
        os.getenv("AGENTD_WORKSPACE_PATH", "/data/agentd-workspaces")
    )


async def get_db_session(state) -> AsyncGenerator[AsyncSession, None]:
    """Get database session for agentd.

    This function reuses the existing database session from the app_server
    configuration if available, otherwise creates a new one.

    Args:
        state: InjectorState from the request

    Yields:
        AsyncSession: An async SQL session
    """
    from openhands.app_server.config import get_global_config
    from openhands.app_server.services.db_session_injector import DB_SESSION_ATTR

    db_session = getattr(state, DB_SESSION_ATTR, None)
    if db_session:
        yield db_session
    else:
        config = get_global_config()
        db_session_injector = config.db

        if db_session_injector is None:
            raise RuntimeError(
                "Database not configured. Please set up DB_HOST, DB_NAME, etc."
            )

        async for session in db_session_injector.inject(state):
            yield session


WORKSPACE_BASE_PATH = get_agentd_workspace_path()


def get_agentd_sqlite_path() -> Path:
    return Path(
        os.getenv("AGENTD_SQLITE_PATH", "/tmp/agent-workspaces/agentd.sqlite3")
    )


def create_agentd_engine(db_path: str | None = None):
    path = Path(db_path) if db_path else get_agentd_sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}", future=True)


def create_agentd_session_factory(db_path: str | None = None) -> sessionmaker[Session]:
    engine = create_agentd_engine(db_path=db_path)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
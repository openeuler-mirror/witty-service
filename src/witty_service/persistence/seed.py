from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from witty_service.domain.enums import AgentKind, SandboxType
from witty_service.domain.records import (
    AgentDefinitionRecord,
    SkillRepoRecord,
    WorkspaceRecord,
)
from witty_service.persistence.entities import utc_ms_now
from witty_service.persistence.repository import (
    AgentRepository,
    CatalogRepository,
    WorkspaceRepository,
)

BUILTIN_SKILL_REPO_NAME = "builtin"


@dataclass(frozen=True, slots=True)
class SeedResult:
    primary_agent: AgentDefinitionRecord
    builtin_skill_repo: SkillRepoRecord
    default_workspace: WorkspaceRecord


def seed_defaults(
    session_factory: sessionmaker[Session], *, host_directory: str | Path
) -> SeedResult:
    agents = AgentRepository(session_factory)
    catalog = CatalogRepository(session_factory)
    workspaces = WorkspaceRepository(session_factory)
    now = utc_ms_now()
    directory = Path(host_directory).expanduser().resolve()

    primary = agents.find_primary_any_state()
    if primary is None:
        try:
            primary = agents.create("Primary", AgentKind.primary, now)
        except IntegrityError:
            primary = agents.get_primary()
            if primary is None:
                raise
    elif primary.archived_at is not None:
        restored = agents.restore_primary(primary.id, now)
        if restored is None:
            raise RuntimeError("failed to restore primary agent")
        primary = restored

    builtin_repo = catalog.get_skill_repo_by_name(BUILTIN_SKILL_REPO_NAME)
    if builtin_repo is None:
        try:
            builtin_repo = catalog.create_skill_repo(
                BUILTIN_SKILL_REPO_NAME, "builtin", now
            )
        except IntegrityError:
            builtin_repo = catalog.get_skill_repo_by_name(BUILTIN_SKILL_REPO_NAME)
            if builtin_repo is None:
                raise

    workspace = workspaces.get_default()
    if workspace is None:
        existing = workspaces.get_by_host_directory(str(directory))
        if existing is not None:
            workspace = workspaces.set_default(existing.id, now)
            if workspace is None:
                raise RuntimeError("failed to set the default workspace")
        else:
            default_model = catalog.get_default_model()
            directory.mkdir(parents=True, exist_ok=True)
            try:
                workspace = workspaces.create(
                    "Default",
                    str(directory),
                    SandboxType.local,
                    primary.id,
                    default_model.id if default_model else None,
                    1,
                    now,
                )
            except IntegrityError:
                workspace = workspaces.get_default()
                if workspace is None:
                    raise

    assert primary is not None and builtin_repo is not None and workspace is not None
    return SeedResult(
        primary_agent=primary,
        builtin_skill_repo=builtin_repo,
        default_workspace=workspace,
    )

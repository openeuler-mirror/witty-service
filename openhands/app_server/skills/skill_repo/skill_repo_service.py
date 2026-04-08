from __future__ import annotations

import io
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from urllib.parse import urlparse
from uuid import uuid4
from zipfile import ZipFile

import frontmatter
from sqlalchemy import Column, String, UniqueConstraint, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from openhands.agent_server.utils import utc_now
from openhands.app_server.config import get_global_config
from openhands.app_server.skills.models.skill_discovery_models import (
    SkillDiscoveryActivationType,
    SkillDiscoveryItem,
    SkillSourceRepo,
)
from openhands.app_server.skills.skill_repo.skill_repo_models import (
    CreateSkillRepoRequest,
    SkillRepo,
    SkillRepoSourceType,
    UpdateSkillRepoRequest,
)
from openhands.app_server.user.user_context import UserContext
from openhands.app_server.utils.sql_utils import (
    Base,
    UtcDateTime,
    create_json_type_decorator,
)

_logger = logging.getLogger(__name__)


class _BackgroundUserContext(UserContext):
    """继承 UserContext 来拿 user_id，只实现 get_user_id，其余抛 NotImplementedError."""

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    async def get_user_id(self) -> str | None:
        return self._user_id

    async def get_user_info(self):
        raise NotImplementedError('Background user context does not support user info')

    async def get_authenticated_git_url(self, repository: str) -> str:
        raise NotImplementedError('Background user context does not support git auth')

    async def get_provider_tokens(self):
        raise NotImplementedError(
            'Background user context does not support provider tokens'
        )

    async def get_latest_token(self, provider_type):
        raise NotImplementedError(
            'Background user context does not support provider tokens'
        )

    async def get_secrets(self):
        raise NotImplementedError('Background user context does not support secrets')

    async def get_mcp_api_key(self) -> str | None:
        raise NotImplementedError('Background user context does not support MCP keys')


class StoredSkillRepo(Base):  # type: ignore
    __tablename__ = 'skill_repo'
    __table_args__ = (
        UniqueConstraint('user_id', 'name', name='uq_skill_repo_user_name'),
    )

    repo_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    source_type = Column(String, nullable=False)
    branch = Column(String, nullable=True)
    url = Column(String, nullable=True)
    local_path = Column(String, nullable=True)
    created_at = Column(UtcDateTime, nullable=False, default=utc_now)
    updated_at = Column(UtcDateTime, nullable=False, default=utc_now)


class StoredSkillRepoDiscoveryCache(Base):  # type: ignore
    __tablename__ = 'skill_repo_discovery_cache'
    __table_args__ = (
        UniqueConstraint('user_id', 'repo_id', name='uq_skill_repo_cache_user_repo'),
    )

    user_id = Column(String, primary_key=True)
    repo_id = Column(String, primary_key=True)
    repo_name = Column(String, nullable=False)
    discover_status = Column(String, nullable=False, default='done')
    payload = Column(
        create_json_type_decorator(list[dict[str, object]]), nullable=False
    )
    updated_at = Column(UtcDateTime, nullable=False, default=utc_now)


@dataclass(frozen=True)
class DiscoveredRepoSkill:
    key: str
    name: str
    activation_type: SkillDiscoveryActivationType
    triggers: list[str]
    origin_path: str
    content: str


@dataclass
class SkillRepoService:
    db_session: AsyncSession
    user_context: UserContext

    async def list_skill_repos(self) -> list[SkillRepo]:
        user_id = await self._require_user_id()
        result = await self.db_session.execute(
            select(StoredSkillRepo)
            .where(StoredSkillRepo.user_id == user_id)
            .order_by(desc(StoredSkillRepo.created_at), StoredSkillRepo.name.asc())
        )
        return [self._to_model(row) for row in result.scalars().all()]

    async def create_skill_repo(self, request: CreateSkillRepoRequest) -> SkillRepo:
        user_id = await self._require_user_id()
        normalized = self._normalize_create_request(request)

        name = self._derive_repo_name(normalized)
        await self._check_unique_name(user_id, name)
        stored = StoredSkillRepo(
            repo_id=str(uuid4()),
            user_id=user_id,
            name=name,
            source_type=normalized.source_type.value,
            branch=normalized.branch,
            url=normalized.url,
            local_path=normalized.local_path,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.db_session.add(stored)
        await self.db_session.commit()
        await self.db_session.refresh(stored)
        return self._to_model(stored)

    @classmethod
    async def discover_repo_in_background(cls, *, repo_id: str, user_id: str) -> None:
        config = get_global_config()
        session_maker = await config.db_session.get_async_session_maker()
        async with session_maker() as session:
            service = cls(
                db_session=session,
                user_context=_BackgroundUserContext(user_id),
            )
            try:
                await service.discover_one_repo_skill(repo_id)
            except Exception as exc:
                _logger.warning(
                    'Background discover failed for repo %s (user %s): %s',
                    repo_id,
                    user_id,
                    exc,
                )

    async def update_skill_repo(
        self, repo_id: str, request: UpdateSkillRepoRequest
    ) -> SkillRepo:
        stored = await self._get_owned_repo(repo_id)

        source_type = request.source_type or self._coerce_source_type(
            stored.source_type
        )
        branch = request.branch.strip() if request.branch is not None else stored.branch
        url = request.url.strip() if request.url is not None else stored.url
        local_path = (
            request.local_path.strip()
            if request.local_path is not None
            else stored.local_path
        )

        self._validate_source_fields(
            source_type=source_type,
            url=url,
            local_path=local_path,
            branch=branch,
        )
        stored.source_type = source_type.value
        stored.branch = branch or None
        stored.url = url or None
        stored.local_path = local_path or None
        stored.updated_at = utc_now()

        await self.db_session.commit()
        await self.db_session.refresh(stored)
        return self._to_model(stored)

    async def delete_skill_repo(self, repo_id: str) -> None:
        stored = await self._get_owned_repo(repo_id)
        await self.db_session.delete(stored)
        await self.db_session.execute(
            StoredSkillRepoDiscoveryCache.__table__.delete().where(
                StoredSkillRepoDiscoveryCache.user_id == stored.user_id,
                StoredSkillRepoDiscoveryCache.repo_id == stored.repo_id,
            )
        )
        await self.db_session.commit()

    async def discover_repos_skill(
        self, *, include_content: bool = False
    ) -> list[SkillDiscoveryItem]:
        user_id = await self._require_user_id()
        repos = await self._list_owned_repos(user_id)
        items: list[SkillDiscoveryItem] = []
        grouped_payload: dict[str, tuple[str, list[dict[str, object]]]] = {}
        statuses: dict[str, str] = {}

        for repo in repos:
            await self._set_discovery_status(repo, 'discovering')
            try:
                repo_skills = self._discover_repo_skills(repo)
                repo_items = [
                    SkillDiscoveryItem(
                        key=skill.key,
                        name=skill.name,
                        activation_type=skill.activation_type,
                        triggers=skill.triggers,
                        origin_path=skill.origin_path,
                        content=skill.content if include_content else None,
                        source_repo=self._build_source_repo(repo),
                        source_ref=repo.branch,
                        readme_url=self._build_readme_url(repo, skill.origin_path),
                    )
                    for skill in repo_skills
                ]
                items.extend(repo_items)
                grouped_payload[repo.repo_id] = (
                    repo.name,
                    [item.model_dump(mode='json') for item in repo_items],
                )
                statuses[repo.repo_id] = 'done'
            except Exception as exc:
                _logger.warning(
                    'Failed to discover skills from repo %s (%s): %s',
                    repo.repo_id,
                    repo.name,
                    exc,
                )
                grouped_payload[repo.repo_id] = (repo.name, [])
                statuses[repo.repo_id] = 'failed'

        await self._store_discovery_cache_for_all_repos(grouped_payload, statuses)
        return items

    async def discover_one_repo_skill(
        self, repo_id: str, *, include_content: bool = False
    ) -> list[SkillDiscoveryItem]:
        stored = await self._get_owned_repo(repo_id)
        await self._set_discovery_status(stored, 'discovering')
        try:
            repo_skills = self._discover_repo_skills(stored)
            items = [
                SkillDiscoveryItem(
                    key=skill.key,
                    name=skill.name,
                    activation_type=skill.activation_type,
                    triggers=skill.triggers,
                    origin_path=skill.origin_path,
                    content=skill.content if include_content else None,
                    source_repo=self._build_source_repo(stored),
                    source_ref=stored.branch,
                    readme_url=self._build_readme_url(stored, skill.origin_path),
                )
                for skill in repo_skills
            ]
            await self._store_discovery_cache_for_one_repo(
                repo_id, items, status='done'
            )
            return items
        except Exception as exc:
            _logger.warning(
                'Failed to discover skills from repo %s (%s): %s',
                stored.repo_id,
                stored.name,
                exc,
            )
            await self._store_discovery_cache_for_one_repo(repo_id, [], status='failed')
            return []

    async def get_discovered_repos_skill(self) -> list[SkillDiscoveryItem]:
        user_id = await self._require_user_id()
        result = await self.db_session.execute(
            select(StoredSkillRepoDiscoveryCache).where(
                StoredSkillRepoDiscoveryCache.user_id == user_id,
            )
        )
        cached_rows = result.scalars().all()
        if not cached_rows:
            return []
        try:
            items: list[SkillDiscoveryItem] = []
            for cached in cached_rows:
                items.extend(
                    SkillDiscoveryItem.model_validate(item)
                    for item in (cached.payload or [])
                )
            return items
        except Exception as exc:
            _logger.warning('Failed to parse discovery cache: %s', exc)
            return []

    async def get_discovered_one_repo_skill(
        self, repo_id: str
    ) -> list[SkillDiscoveryItem]:
        # Ensure repo exists and belongs to the user.
        _ = await self._get_owned_repo(repo_id)
        user_id = await self._require_user_id()
        result = await self.db_session.execute(
            select(StoredSkillRepoDiscoveryCache).where(
                StoredSkillRepoDiscoveryCache.user_id == user_id,
                StoredSkillRepoDiscoveryCache.repo_id == repo_id,
            )
        )
        cached = result.scalar_one_or_none()
        if cached is None:
            return []
        try:
            return [
                SkillDiscoveryItem.model_validate(item)
                for item in (cached.payload or [])
            ]
        except Exception as exc:
            _logger.warning('Failed to parse discovery cache: %s', exc)
            return []

    async def get_discover_status(self) -> list[dict[str, str]]:
        user_id = await self._require_user_id()
        result = await self.db_session.execute(
            select(StoredSkillRepoDiscoveryCache).where(
                StoredSkillRepoDiscoveryCache.user_id == user_id,
            )
        )
        rows = result.scalars().all()
        return [
            {
                'repo_id': row.repo_id,
                'repo_name': row.repo_name,
                'discover_status': row.discover_status,
            }
            for row in rows
        ]

    async def _discover_all_repos_skill(
        self, *, include_content: bool
    ) -> list[SkillDiscoveryItem]:
        user_id = await self._require_user_id()
        result = await self.db_session.execute(
            select(StoredSkillRepo)
            .where(
                StoredSkillRepo.user_id == user_id,
            )
            .order_by(StoredSkillRepo.created_at.desc(), StoredSkillRepo.name.asc())
        )

        items: list[SkillDiscoveryItem] = []
        for repo in result.scalars().all():
            try:
                repo_skills = self._discover_repo_skills(repo)
            except Exception as exc:
                _logger.warning(
                    'Failed to discover skills from repo %s (%s): %s',
                    repo.repo_id,
                    repo.name,
                    exc,
                )
                continue

            for skill in repo_skills:
                items.append(
                    SkillDiscoveryItem(
                        key=skill.key,
                        name=skill.name,
                        activation_type=skill.activation_type,
                        triggers=skill.triggers,
                        origin_path=skill.origin_path,
                        content=skill.content if include_content else None,
                        source_repo=self._build_source_repo(repo),
                        source_ref=repo.branch,
                        readme_url=self._build_readme_url(repo, skill.origin_path),
                    )
                )

        return items

    async def _store_discovery_cache_for_all_repos(
        self,
        grouped_payload: dict[str, tuple[str, list[dict[str, object]]]],
        statuses: dict[str, str],
    ) -> None:
        user_id = await self._require_user_id()

        owned_repo_ids = {
            repo.repo_id for repo in await self._list_owned_repos(user_id)
        }
        await self.db_session.execute(
            StoredSkillRepoDiscoveryCache.__table__.delete().where(
                StoredSkillRepoDiscoveryCache.user_id == user_id,
            )
        )
        for repo_id, (repo_name, payload) in grouped_payload.items():
            if repo_id not in owned_repo_ids:
                _logger.info(
                    'Skip caching discovery result for repo %s (%s): not owned',
                    repo_id,
                    repo_name,
                )
                continue
            self.db_session.add(
                StoredSkillRepoDiscoveryCache(
                    user_id=user_id,
                    repo_id=repo_id,
                    repo_name=repo_name,
                    discover_status=statuses.get(repo_id, 'done'),
                    payload=payload,
                    updated_at=utc_now(),
                )
            )
        await self.db_session.commit()

    async def _store_discovery_cache_for_one_repo(
        self, repo_id: str, items: list[SkillDiscoveryItem], *, status: str
    ) -> None:
        user_id = await self._require_user_id()
        try:
            stored = await self._get_owned_repo(repo_id)
        except KeyError:
            _logger.info(
                'Skip caching discovery result for repo %s: not owned',
                repo_id,
            )
            return
        repo_name = stored.name
        payload = [item.model_dump(mode='json') for item in items]
        result = await self.db_session.execute(
            select(StoredSkillRepoDiscoveryCache).where(
                StoredSkillRepoDiscoveryCache.user_id == user_id,
                StoredSkillRepoDiscoveryCache.repo_id == repo_id,
            )
        )
        cached = result.scalar_one_or_none()
        if cached is None:
            cached = StoredSkillRepoDiscoveryCache(
                user_id=user_id,
                repo_id=repo_id,
                repo_name=repo_name,
                discover_status=status,
                payload=payload,
                updated_at=utc_now(),
            )
            self.db_session.add(cached)
        else:
            cached.repo_name = repo_name
            cached.discover_status = status
            cached.payload = payload
            cached.updated_at = utc_now()
        await self.db_session.commit()

    async def get_repo_skill_by_key(
        self, *, repo_key: str, include_content: bool = True
    ) -> SkillDiscoveryItem | None:
        parsed = self._parse_repo_key(repo_key)
        if parsed is None:
            raise ValueError(f'Invalid repo_key: {repo_key}')
        repo_id, _ = parsed

        stored = await self._get_owned_repo(repo_id)
        try:
            repo_skills = self._discover_repo_skills(stored)
        except Exception as exc:
            _logger.warning(
                'Failed to discover skills from repo %s (%s): %s',
                stored.repo_id,
                stored.name,
                exc,
            )
            return None

        for skill in repo_skills:
            if skill.key != repo_key:
                continue
            return SkillDiscoveryItem(
                key=skill.key,
                name=skill.name,
                activation_type=skill.activation_type,
                triggers=skill.triggers,
                origin_path=skill.origin_path,
                content=skill.content if include_content else None,
                source_repo=self._build_source_repo(stored),
                source_ref=stored.branch,
                readme_url=self._build_readme_url(stored, skill.origin_path),
            )
        return None

    async def _get_owned_repo(self, repo_id: str) -> StoredSkillRepo:
        user_id = await self._require_user_id()
        result = await self.db_session.execute(
            select(StoredSkillRepo).where(
                StoredSkillRepo.repo_id == repo_id,
                StoredSkillRepo.user_id == user_id,
            )
        )
        stored = result.scalar_one_or_none()
        if stored is None:
            raise KeyError(f'Skill repo {repo_id} not found')
        return stored

    async def _list_owned_repos(self, user_id: str) -> list[StoredSkillRepo]:
        result = await self.db_session.execute(
            select(StoredSkillRepo)
            .where(StoredSkillRepo.user_id == user_id)
            .order_by(StoredSkillRepo.created_at.desc(), StoredSkillRepo.name.asc())
        )
        return result.scalars().all()

    async def _set_discovery_status(self, repo: StoredSkillRepo, status: str) -> None:
        user_id = await self._require_user_id()
        result = await self.db_session.execute(
            select(StoredSkillRepoDiscoveryCache).where(
                StoredSkillRepoDiscoveryCache.user_id == user_id,
                StoredSkillRepoDiscoveryCache.repo_id == repo.repo_id,
            )
        )
        cached = result.scalar_one_or_none()
        if cached is None:
            cached = StoredSkillRepoDiscoveryCache(
                user_id=user_id,
                repo_id=repo.repo_id,
                repo_name=repo.name,
                discover_status=status,
                payload=[],
                updated_at=utc_now(),
            )
            self.db_session.add(cached)
        else:
            cached.repo_name = repo.name
            cached.discover_status = status
            cached.updated_at = utc_now()
        await self.db_session.commit()

    async def _require_user_id(self) -> str:
        user_id = await self.user_context.get_user_id()
        if user_id is None:
            user_id = 'anonymous'  # Temporary fallback for unauthenticated users; eventually enforce auth.
            # raise PermissionError('Not authenticated')
        return user_id

    def _normalize_create_request(
        self, request: CreateSkillRepoRequest
    ) -> CreateSkillRepoRequest:
        local_path = request.local_path
        url = request.url
        normalized = request.model_copy(
            update={
                'branch': request.branch.strip()
                if request.branch is not None
                else None,
                'url': url.strip() if url is not None else None,
                'local_path': local_path.strip() if local_path is not None else None,
            }
        )
        self._validate_source_fields(
            source_type=normalized.source_type,
            url=normalized.url,
            local_path=normalized.local_path,
            branch=normalized.branch,
        )
        return normalized

    def _derive_repo_name(self, request: CreateSkillRepoRequest) -> str:
        source_type = request.source_type
        if source_type == SkillRepoSourceType.GIT:
            owner, repo_name = self._parse_git_owner_repo(request.url)
            branch = request.branch or ''
            return f'git:{owner}/{repo_name}@{branch}'
        if source_type == SkillRepoSourceType.LOCAL_IMPORT and request.local_path:
            local_path = Path(request.local_path).expanduser()
            if local_path.is_file() and local_path.suffix == '.zip':
                repo_name = local_path.stem or 'local-zip'
            else:
                repo_name = local_path.name or 'local-repo'
            return f'local:{repo_name}'
        return f'{source_type.value}-repo'

    async def _check_unique_name(self, user_id: str, repo_name: str) -> None:
        result = await self.db_session.execute(
            select(StoredSkillRepo).where(
                StoredSkillRepo.user_id == user_id,
                StoredSkillRepo.name == repo_name,
            )
        )
        if result.scalar_one_or_none() is not None:
            raise ValueError(f'Skill repo "{repo_name}" already exists')

    def _validate_source_fields(
        self,
        source_type: SkillRepoSourceType,
        url: str | None,
        local_path: str | None,
        branch: str | None,
    ) -> None:
        if source_type == SkillRepoSourceType.GIT:
            if not url:
                raise ValueError(f'{source_type.value} skill repos require url')
            if not branch:
                raise ValueError(f'{source_type.value} skill repos require branch')
            return
        if source_type == SkillRepoSourceType.LOCAL_IMPORT:
            if not local_path:
                raise ValueError('local_import skill repos require local_path')
            return

    def _to_model(self, stored: StoredSkillRepo) -> SkillRepo:
        source_type = self._coerce_source_type(stored.source_type)
        return SkillRepo(
            repo_id=stored.repo_id,
            name=stored.name,
            source_type=source_type,
            branch=stored.branch,
            url=stored.url,
            local_path=stored.local_path,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
        )

    def _parse_repo_key(self, repo_key: str) -> tuple[str, str] | None:
        parts = repo_key.split(':', maxsplit=2)
        if len(parts) != 3 or parts[0] != 'repo':
            return None
        return parts[1], parts[2]

    def _coerce_source_type(self, source_type: str) -> SkillRepoSourceType:
        # Treat legacy zip_url/local_dir as local_import for backward compatibility.
        if source_type in {'zip_url', 'local_dir'}:
            return SkillRepoSourceType.LOCAL_IMPORT
        return SkillRepoSourceType(source_type)

    def _discover_repo_skills(self, repo: StoredSkillRepo) -> list[DiscoveredRepoSkill]:
        source_type = self._coerce_source_type(repo.source_type)
        if source_type == SkillRepoSourceType.LOCAL_IMPORT:
            if repo.local_path is None:
                return []
            local_path = Path(repo.local_path).expanduser().resolve(strict=False)
            if local_path.is_file() and local_path.suffix == '.zip':
                return self._discover_local_archive_skills(repo, local_path)
            return self._scan_local_repo(repo=repo, repo_root=local_path)
        return self._discover_git_repo_skills(repo)

    def _discover_git_repo_skills(
        self, repo: StoredSkillRepo
    ) -> list[DiscoveredRepoSkill]:
        if repo.url is None:
            return []

        with TemporaryDirectory() as temp_dir:
            clone_dir = Path(temp_dir) / 'repo'
            command = ['git', 'clone', '--depth', '1']
            if repo.branch:
                command.extend(['--branch', repo.branch])
            command.extend([repo.url, str(clone_dir)])
            subprocess.run(command, check=True, capture_output=True, text=True)
            return self._scan_git_repo(repo=repo, repo_root=clone_dir)

    def _discover_local_archive_skills(
        self, repo: StoredSkillRepo, archive_path: Path
    ) -> list[DiscoveredRepoSkill]:
        with TemporaryDirectory() as temp_dir:
            extract_dir = Path(temp_dir) / 'archive'
            extract_dir.mkdir(parents=True, exist_ok=True)
            with ZipFile(archive_path) as archive:
                archive.extractall(extract_dir)

            repo_root = self._find_archive_repo_root(extract_dir)
            return self._scan_local_repo(repo=repo, repo_root=repo_root)

    def _find_archive_repo_root(self, extract_dir: Path) -> Path:
        children = [child for child in extract_dir.iterdir() if child.is_dir()]
        if len(children) == 1:
            return children[0]
        return extract_dir

    def _scan_git_repo(
        self, repo: StoredSkillRepo, repo_root: Path
    ) -> list[DiscoveredRepoSkill]:
        owner, repo_name = self._parse_git_owner_repo(repo.url)
        branch = repo.branch or 'main'
        prefix = f'git:{owner}/{repo_name}@{branch}'
        return self._scan_repo_root(
            repo=repo,
            repo_root=repo_root,
            name_builder=lambda skill_name, repo_name_hint: f'{prefix}/{skill_name}',
            only_root=False,
        )

    def _scan_local_repo(
        self, repo: StoredSkillRepo, repo_root: Path
    ) -> list[DiscoveredRepoSkill]:
        # If root contains SKILL.md, treat it as a single-skill import.
        root_skill = repo_root / 'SKILL.md'
        if root_skill.exists():
            return self._scan_repo_root(
                repo=repo,
                repo_root=repo_root,
                name_builder=lambda skill_name, repo_name_hint: f'local:{skill_name}',
                only_root=True,
            )
        repo_name_hint = repo_root.name or 'repo'

        def _local_name_builder(
            skill_name: str, repo_name_hint: str = repo_name_hint
        ) -> str:
            return f'local:{repo_name_hint}/{skill_name}'

        return self._scan_repo_root(
            repo=repo,
            repo_root=repo_root,
            name_builder=_local_name_builder,
            only_root=False,
        )

    def _scan_repo_root(
        self,
        repo: StoredSkillRepo,
        repo_root: Path,
        *,
        name_builder: Callable[[str, str], str],
        only_root: bool,
    ) -> list[DiscoveredRepoSkill]:
        if not repo_root.exists():
            return []

        if only_root:
            skill_files = [repo_root / 'SKILL.md']
        else:
            skill_files = sorted(
                path
                for path in repo_root.rglob('*')
                if path.is_file() and path.name == 'SKILL.md'
            )

        discovered: list[DiscoveredRepoSkill] = []
        for skill_file in skill_files:
            if not skill_file.exists():
                continue
            loaded = frontmatter.load(
                io.StringIO(skill_file.read_text(encoding='utf-8'))
            )
            metadata = loaded.metadata or {}
            triggers = self._normalize_repo_triggers(metadata.get('triggers'))
            skill_name = self._derive_repo_skill_name(skill_file, metadata)
            discovered.append(
                DiscoveredRepoSkill(
                    key=self._build_repo_skill_key(repo, repo_root, skill_file),
                    name=name_builder(skill_name, repo_root.name),
                    activation_type=self._get_repo_activation_type(
                        metadata=metadata,
                        triggers=triggers,
                    ),
                    triggers=triggers,
                    origin_path=self._to_repo_relative_path(repo_root, skill_file),
                    content=loaded.content,
                )
            )
        return discovered

    def _parse_git_owner_repo(self, url: str | None) -> tuple[str, str]:
        # Best-effort parsing for common git URL formats.
        if not url:
            return ('unknown', 'repo')
        if url.startswith('git@'):
            # git@host:owner/repo.git
            parts = url.split(':', maxsplit=1)
            path = parts[1] if len(parts) > 1 else ''
        else:
            parsed = urlparse(url)
            path = parsed.path
        path = path.strip('/').removesuffix('.git')
        chunks = [chunk for chunk in path.split('/') if chunk]
        if len(chunks) >= 2:
            return (chunks[-2], chunks[-1])
        if len(chunks) == 1:
            return ('unknown', chunks[0])
        return ('unknown', 'repo')

    def _normalize_repo_triggers(self, raw_triggers: object) -> list[str]:
        if isinstance(raw_triggers, str):
            return [raw_triggers.strip()] if raw_triggers.strip() else []
        if not isinstance(raw_triggers, list):
            return []

        normalized: list[str] = []
        for raw_trigger in raw_triggers:
            if not isinstance(raw_trigger, str):
                continue
            cleaned = raw_trigger.strip()
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return normalized

    def _get_repo_activation_type(
        self, metadata: dict[str, object], triggers: list[str]
    ) -> SkillDiscoveryActivationType:
        if metadata.get('type') == 'task' or metadata.get('inputs'):
            return SkillDiscoveryActivationType.TASK
        if triggers:
            return SkillDiscoveryActivationType.TRIGGERED
        return SkillDiscoveryActivationType.ALWAYS

    def _derive_repo_skill_name(
        self, skill_file: Path, metadata: dict[str, object]
    ) -> str:
        metadata_name = metadata.get('name')
        if isinstance(metadata_name, str) and metadata_name.strip():
            return metadata_name.strip()
        if skill_file.name == 'SKILL.md':
            return skill_file.parent.name
        return skill_file.stem

    def _build_repo_skill_key(
        self, repo: StoredSkillRepo, repo_root: Path, skill_file: Path
    ) -> str:
        relative_path = skill_file.relative_to(repo_root)
        if relative_path.name == 'SKILL.md':
            slug_path = relative_path.parent
        else:
            slug_path = relative_path.with_suffix('')

        slug = slug_path.as_posix() if str(slug_path) not in {'', '.'} else 'skill'
        return f'repo:{repo.repo_id}:{slug}'

    def _to_repo_relative_path(self, repo_root: Path, skill_file: Path) -> str:
        return skill_file.relative_to(repo_root).as_posix()

    def _build_source_repo(self, repo: StoredSkillRepo) -> SkillSourceRepo:
        return SkillSourceRepo(
            repo_id=repo.repo_id,
            name=repo.name,
            source_type=self._coerce_source_type(repo.source_type),
            branch=repo.branch,
            url=repo.url,
            local_path=repo.local_path,
        )

    def _build_readme_url(self, repo: StoredSkillRepo, origin_path: str) -> str | None:
        source_type = self._coerce_source_type(repo.source_type)
        if source_type == SkillRepoSourceType.GIT and repo.url is not None:
            return repo.url
        return None

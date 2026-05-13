from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from uuid import uuid4
from zipfile import ZipFile

from src.api.schemas import SkillRepositoryRequest, SkillRepositorySourceType
from src.persistence.repositories import SkillRepositoryRecord, SqliteRepository

_logger = logging.getLogger(__name__)
GIT_CLONE_RETRY_TIMES = 3
LOCAL_ARCHIVE_BASE_DIR = Path('/tmp/witty-service-skill-repo-archives')


@dataclass(slots=True, frozen=True)
class SkillMinRepository:
    repo_id: str
    name: str
    source_type: SkillRepositorySourceType
    branch: str | None = None
    url: str | None = None
    local_path: str | None = None


@dataclass(slots=True, frozen=True)
class SkillObject:
    skill_id: str
    skill_name: str
    relative_path: str | None = None
    metadata: dict[str, object] | None = None
    source_repo: SkillMinRepository | None = None
    skill_md_url: str | None = None


@dataclass(slots=True)
class SkillManager:
    repository: SqliteRepository

    def list_skill_repositories(self) -> list[SkillRepositoryRecord]:
        return self.repository.list_skill_repositories()

    def create_skill_repository(
        self, request: SkillRepositoryRequest
    ) -> SkillRepositoryRecord:
        normalized = self._normalize_create_request(request)
        repository_name = self._derive_repository_name(normalized)
        existing = self.repository.get_skill_repository_by_name(repository_name)
        if existing is not None:
            raise ValueError(
                f'Skill repository "{repository_name}" already exists with repo_id "{existing.repo_id}"'
            )
        return self.repository.create_skill_repository(
            name=repository_name,
            source_type=normalized.source_type,
            branch=normalized.branch,
            url=normalized.url,
            local_path=normalized.local_path,
        )

    def update_skill_repository(
        self,
        repo_id: str,
        request: SkillRepositoryRequest,
    ) -> SkillRepositoryRecord:
        stored = self._get_owned_repo(repo_id)
        source_type = request.source_type or stored.source_type
        branch = request.branch.strip() if request.branch is not None else stored.branch
        url = (
            self._normalize_git_clone_url(request.url.strip())
            if request.url is not None
            else stored.url
        )
        local_path = (
            request.local_path.strip()
            if request.local_path is not None
            else stored.local_path
        )

        self._validate_source_fields(
            source_type=source_type,
            url=url,
            local_path=local_path,
        )
        return self.repository.update_skill_repository(
            repo_id,
            source_type=source_type.value,
            branch=branch or None,
            url=url or None,
            local_path=local_path or None,
        )

    def delete_skill_repository(self, repo_id: str) -> None:
        self.repository.delete_skill_repository(repo_id)

    def discover_skill_repositories(self) -> list[SkillRepositoryRecord]:
        updated_repositories: list[SkillRepositoryRecord] = []
        for repository in self.repository.list_skill_repositories():
            self._set_discovery_status(repository, 'discovering')
            try:
                repository_skills = self._discover_skill_repository_skills(repository)
                updated_repositories.append(
                    self.repository.update_skill_repository_discovery(
                        repository.repo_id,
                        skill_discover_status='done',
                        skill_num=len(repository_skills),
                        discovered_skills=[
                            self._discovery_item_to_payload(item)
                            for item in repository_skills
                        ],
                    )
                )
            except Exception as exc:
                _logger.warning(
                    'Failed to discover skills from repository %s (%s): %s',
                    repository.repo_id,
                    repository.repo_name,
                    exc,
                )
                updated_repositories.append(
                    self.repository.update_skill_repository_discovery(
                        repository.repo_id,
                        skill_discover_status='failed',
                        skill_num=0,
                        discovered_skills=[],
                    )
                )
        return updated_repositories

    def discover_one_skill_repository(self, repo_id: str) -> SkillRepositoryRecord:
        repository = self._get_owned_repo(repo_id)
        if repository.skill_discover_status == 'discovering':
            raise ValueError('Skill repository discovery is already in progress')

        self._set_discovery_status(repository, 'discovering')
        try:
            items = self._discover_skill_repository_skills(repository)
            return self.repository.update_skill_repository_discovery(
                repository.repo_id,
                skill_discover_status='done',
                skill_num=len(items),
                discovered_skills=[
                    self._discovery_item_to_payload(item) for item in items
                ],
            )
        except Exception:
            self.repository.update_skill_repository_discovery(
                repository.repo_id,
                skill_discover_status='failed',
                skill_num=0,
                discovered_skills=[],
            )
            raise

    def get_repository(self, repo_id: str) -> SkillRepositoryRecord:
        return self._get_owned_repo(repo_id)

    @classmethod
    def discover_skill_repository_in_background(
        cls,
        *,
        repository: SqliteRepository,
        repo_id: str,
    ) -> None:
        service = cls(repository=repository)
        try:
            service.discover_one_skill_repository(repo_id)
        except Exception as exc:
            _logger.warning(
                'Background discover failed for repository %s: %s', repo_id, exc
            )

    def _get_owned_repo(self, repo_id: str) -> SkillRepositoryRecord:
        repository = self.repository.get_skill_repository(repo_id)
        if repository is None:
            raise KeyError(f'Skill repository {repo_id} not found')
        return repository

    def _set_discovery_status(self, repo: SkillRepositoryRecord, status: str) -> None:
        self.repository.update_skill_repository_discovery(
            repo.repo_id,
            skill_discover_status=status,
            skill_num=repo.skill_num,
            discovered_skills=repo.discovered_skills,
        )

    def _normalize_create_request(
        self, request: SkillRepositoryRequest
    ) -> SkillRepositoryRequest:
        if request.source_type is None:
            raise ValueError('source_type is required')

        branch = request.branch.strip() if request.branch is not None else None
        url = (
            self._normalize_git_clone_url(request.url.strip())
            if request.url is not None
            else None
        )
        local_path = (
            request.local_path.strip() if request.local_path is not None else None
        )
        self._validate_source_fields(
            source_type=request.source_type,
            url=url,
            local_path=local_path,
        )
        return SkillRepositoryRequest(
            source_type=request.source_type,
            branch=branch,
            url=url,
            local_path=local_path,
        )

    def _normalize_git_clone_url(self, url: str | None) -> str:
        if not url:
            return ''
        stripped = url.strip()

        ssh_match = re.match(r'git@([^:]+):(.+)', stripped)
        if ssh_match:
            host = ssh_match.group(1)
            path = ssh_match.group(2).strip('/')
            return f'git@{host}:{path}'

        parsed = urlparse(stripped)
        if not parsed.scheme or not parsed.netloc:
            return ''

        path = parsed.path.strip('/')
        if path.endswith('.git'):
            path = path[:-4]
        return f'{parsed.scheme}://{parsed.netloc}/{path}'

    def _derive_repository_name(self, request: SkillRepositoryRequest) -> str:
        if request.source_type is None:
            raise ValueError('source_type is required')

        if request.source_type == SkillRepositorySourceType.GIT:
            if not request.url:
                raise ValueError('git skill repositories require url')
            normalized_url = request.url.removesuffix('.git')
            if request.branch is None:
                return normalized_url
            return f'{normalized_url}@{request.branch}'

        local_path_str = request.local_path
        if not local_path_str:
            raise ValueError('local_import skill repositories require local_path')
        local_path = Path(local_path_str).expanduser()
        if local_path.is_file() and local_path.suffix == '.zip':
            repository_name = local_path.stem or 'local-repository-zip'
        else:
            repository_name = local_path.name or 'local-repository'
        return f'local:{repository_name}'

    def _validate_source_fields(
        self,
        *,
        source_type: SkillRepositorySourceType,
        url: str | None,
        local_path: str | None,
    ) -> None:
        if source_type == SkillRepositorySourceType.GIT:
            if not url:
                raise ValueError('git skill repositories require url')
            return
        if source_type == SkillRepositorySourceType.LOCAL_IMPORT:
            if not local_path:
                raise ValueError('local_import skill repositories require local_path')
            return
        raise ValueError(f'Unsupported skill repository source type: {source_type}')

    def _discover_skill_repository_skills(
        self, repo: SkillRepositoryRecord
    ) -> list[SkillObject]:
        if repo.source_type == SkillRepositorySourceType.LOCAL_IMPORT:
            return self._discover_local_skill_repository_skills(repo)
        return self._discover_git_skill_repository_skills(repo)

    def _discover_git_skill_repository_skills(
        self, repo: SkillRepositoryRecord
    ) -> list[SkillObject]:
        clone_url = self._normalize_clone_url_for_git(repo.url)
        with TemporaryDirectory() as temp_dir:
            clone_dir = Path(temp_dir) / 'repo'
            command = ['git', 'clone', '--depth', '1']
            if repo.branch:
                command.extend(['--branch', repo.branch])
            command.extend([clone_url, str(clone_dir)])
            last_exc: subprocess.CalledProcessError | None = None
            for _ in range(GIT_CLONE_RETRY_TIMES):
                try:
                    subprocess.run(command, check=True, capture_output=True, text=True)
                    break
                except subprocess.CalledProcessError as exc:
                    last_exc = exc
            else:
                assert last_exc is not None
                raise last_exc

            branch = repo.branch or self._get_cloned_repo_branch(clone_dir)
            resolved_repo = repo
            if branch and branch != repo.branch:
                resolved_repo = SkillRepositoryRecord(
                    repo_id=repo.repo_id,
                    repo_name=repo.repo_name,
                    source_type=repo.source_type,
                    branch=branch,
                    url=repo.url,
                    local_path=repo.local_path,
                    skill_discover_status=repo.skill_discover_status,
                    skill_num=repo.skill_num,
                    discovered_skills=repo.discovered_skills,
                    created_at=repo.created_at,
                    updated_at=repo.updated_at,
                )
            return self._scan_skill_repository_root(
                repo=resolved_repo,
                repo_root=clone_dir,
                only_root=False,
            )

    def _get_cloned_repo_branch(self, clone_dir: Path) -> str | None:
        try:
            result = subprocess.run(
                ['git', '-C', str(clone_dir), 'rev-parse', '--abbrev-ref', 'HEAD'],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            return None
        branch = result.stdout.strip()
        if not branch or branch == 'HEAD':
            return None
        return branch

    def _normalize_clone_url_for_git(self, repo_url: str | None) -> str:
        if not repo_url:
            raise ValueError('git skill repositories require url')
        if repo_url.endswith('.git'):
            return repo_url
        return f'{repo_url}.git'

    def _discover_local_skill_repository_skills(
        self, repo: SkillRepositoryRecord
    ) -> list[SkillObject]:
        local_path = Path(str(repo.local_path)).expanduser().resolve(strict=False)
        if local_path.is_file() and local_path.suffix == '.zip':
            extract_dir = self._prepare_archive_extract_dir(repo)
            repo_root = self._extract_local_archive_to_dir(
                repo, local_path, extract_dir
            )
            return self._scan_local_skill_repository_root(repo, repo_root)
        return self._scan_local_skill_repository_root(repo, local_path)

    def _prepare_archive_extract_dir(self, repo: SkillRepositoryRecord) -> Path:
        extract_dir = LOCAL_ARCHIVE_BASE_DIR / repo.repo_id
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        return extract_dir

    def _extract_local_archive_to_dir(
        self,
        repo: SkillRepositoryRecord,
        archive_path: Path,
        extract_dir: Path,
    ) -> Path:
        del repo
        with ZipFile(archive_path) as archive:
            archive.extractall(extract_dir)
        return self._find_archive_repo_root(extract_dir)

    def _find_archive_repo_root(self, extract_dir: Path) -> Path:
        children = [child for child in extract_dir.iterdir() if child.is_dir()]
        if len(children) == 1:
            return children[0]
        return extract_dir

    def _scan_local_skill_repository_root(
        self, repo: SkillRepositoryRecord, repo_root: Path
    ) -> list[SkillObject]:
        root_skill = repo_root / 'SKILL.md'
        if root_skill.exists():
            return self._scan_skill_repository_root(
                repo=repo,
                repo_root=repo_root,
                only_root=True,
            )
        return self._scan_skill_repository_root(
            repo=repo,
            repo_root=repo_root,
            only_root=False,
        )

    def _scan_skill_repository_root(
        self,
        repo: SkillRepositoryRecord,
        repo_root: Path,
        only_root: bool,
    ) -> list[SkillObject]:
        if not repo_root.exists():
            raise ValueError(
                f'Repository root does not exist for repo_id {repo.repo_id}: {repo_root}'
            )

        if only_root:
            skill_files = [repo_root / 'SKILL.md']
        else:
            skill_files = sorted(
                path
                for path in repo_root.rglob('*')
                if path.is_file() and path.name == 'SKILL.md'
            )

        source_repo = self._build_source_repository(repo)
        discovered: list[SkillObject] = []
        for skill_file in skill_files:
            metadata, _content = self._load_skill_frontmatter(skill_file)
            relative_path = self._to_repository_relative_path(repo_root, skill_file)
            discovered.append(
                SkillObject(
                    skill_id=str(uuid4()),
                    skill_name=self._derive_repository_skill_name(skill_file, metadata),
                    relative_path=relative_path,
                    metadata=metadata,
                    source_repo=source_repo,
                    skill_md_url=self._build_skill_md_url(
                        repo, relative_path, repo_root
                    ),
                )
            )
        return discovered

    def _load_skill_frontmatter(
        self, skill_file: Path
    ) -> tuple[dict[str, object], str]:
        text = skill_file.read_text(encoding='utf-8')
        stripped = text.lstrip()
        if not stripped.startswith('---'):
            return {}, text.strip()
        parts = stripped.split('---', maxsplit=2)
        if len(parts) < 3:
            return {}, text.strip()

        raw_frontmatter = parts[1]
        content = parts[2].lstrip('\r\n')

        metadata: dict[str, object] = {}
        current_key: str | None = None
        for line in raw_frontmatter.splitlines():
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith('#'):
                continue

            if stripped_line.startswith('- ') and current_key == 'triggers':
                triggers = metadata.setdefault('triggers', [])
                if isinstance(triggers, list):
                    trigger = stripped_line[2:].strip()
                    if trigger:
                        triggers.append(trigger)
                continue

            if ':' not in line:
                if current_key is not None:
                    existing = metadata.get(current_key)
                    if isinstance(existing, str):
                        metadata[current_key] = f'{existing} {stripped_line}'.strip()
                continue

            key, value = line.split(':', 1)
            current_key = key.strip()
            metadata[current_key] = self._parse_frontmatter_value(
                current_key, value.strip()
            )

        return metadata, content.strip()

    def _parse_frontmatter_value(self, key: str, value: str) -> object:
        if not value:
            return [] if key == 'triggers' else ''

        if key == 'triggers':
            if value.startswith('[') and value.endswith(']'):
                try:
                    parsed = json.loads(value.replace("'", '"'))
                except Exception:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            return [value.strip('"\'')] if value.strip('"\'') else []

        lowered = value.lower()
        if lowered == 'true':
            return True
        if lowered == 'false':
            return False
        return value.strip('"\'')

    def _derive_repository_skill_name(
        self, skill_file: Path, metadata: dict[str, object]
    ) -> str:
        metadata_name = metadata.get('name')
        if isinstance(metadata_name, str) and metadata_name.strip():
            return metadata_name.strip()
        if skill_file.name == 'SKILL.md':
            return skill_file.parent.name
        return skill_file.stem

    def _to_repository_relative_path(self, repo_root: Path, skill_file: Path) -> str:
        return skill_file.relative_to(repo_root).as_posix()

    def _build_source_repository(
        self, repo: SkillRepositoryRecord
    ) -> SkillMinRepository:
        return SkillMinRepository(
            repo_id=repo.repo_id,
            name=repo.repo_name,
            source_type=repo.source_type,
            branch=repo.branch,
            url=repo.url,
            local_path=repo.local_path,
        )

    def _build_skill_md_url(
        self,
        repo: SkillRepositoryRecord,
        relative_path: str,
        repo_root: Path,
    ) -> str | None:
        if repo.source_type == SkillRepositorySourceType.LOCAL_IMPORT:
            return str((repo_root / relative_path).resolve(strict=False))

        if not repo.url:
            return None
        browse_base_url = self._normalize_repository_browse_base_url(repo.url)
        if not browse_base_url:
            return None
        branch = repo.branch or 'HEAD'
        cleaned_relative_path = relative_path.lstrip('/')
        return f'{browse_base_url}/blob/{branch}/{cleaned_relative_path}'

    def _normalize_repository_browse_base_url(self, repo_url: str) -> str | None:
        normalized_url = repo_url.strip()
        if not normalized_url:
            return None

        ssh_match = re.match(r'git@([^:]+):(.+)', normalized_url)
        if ssh_match:
            host = ssh_match.group(1)
            path = ssh_match.group(2).strip('/')
            if path.endswith('.git'):
                path = path[:-4]
            return f'https://{host}/{path}'

        parsed = urlparse(normalized_url)
        if not parsed.netloc:
            return None
        path = parsed.path.strip('/')
        if path.endswith('.git'):
            path = path[:-4]
        if not path:
            return None
        return f'{parsed.scheme}://{parsed.netloc}/{path}'

    def _discovery_item_to_payload(self, item: SkillObject) -> dict[str, object]:
        payload: dict[str, object] = {
            'skill_id': item.skill_id,
            'skill_name': item.skill_name,
            'relative_path': item.relative_path,
            'metadata': item.metadata or {},
            'skill_md_url': item.skill_md_url,
        }
        if item.source_repo is not None:
            payload['source_repo'] = {
                'repo_id': item.source_repo.repo_id,
                'name': item.source_repo.name,
                'source_type': item.source_repo.source_type,
                'branch': item.source_repo.branch,
                'url': item.source_repo.url,
                'local_path': item.source_repo.local_path,
            }
        return payload

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5
from zipfile import BadZipFile, ZipFile

from fastapi import UploadFile

from witty_service.api.schemas import SkillRepositoryRequest, SkillSourceType
from witty_service.application.awesome_openclaw_sync import (
    AWESOME_REPO_URL,
    is_awesome_openclaw_repository,
    sync_awesome_openclaw_skills,
)
from witty_service.config import get_settings
from witty_service.persistence.repositories import (
    SkillRecord,
    SkillRepositoryRecord,
    SqliteRepository,
)

_logger = logging.getLogger(__name__)
GIT_CLONE_RETRY_TIMES = 3
ZIP_MAX_ARCHIVE_SIZE = 50 * 1024 * 1024  # 上传包大小限制
ZIP_MAX_EXTRACTED_SIZE = 100 * 1024 * 1024  # 解压后总大小限制
ZIP_MAX_ENTRY_COUNT = 1000  # 文件个数限制


class SkillDiscoverStatus:
    INIT = "init"
    DISCOVERING = "discovering"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=False)
class SkillManager:
    repository: SqliteRepository

    def __post_init__(self) -> None:
        settings = get_settings()
        workspace_root = settings.workspace.root_path()
        self._skill_archives_dir = workspace_root / "skill-repositories"
        self._skill_archives_dir.mkdir(parents=True, exist_ok=True)

    def list_skill_repositories(self) -> list[SkillRepositoryRecord]:
        return self.repository.list_skill_repositories()

    def create_skill_repository_from_git(
        self, request: SkillRepositoryRequest
    ) -> SkillRepositoryRecord:
        normalized = self._normalize_create_request(request)
        repository_name = self._derive_git_repository_name(normalized)
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
            skill_discover_status=SkillDiscoverStatus.INIT,
        )

    def update_skill_repository(
        self,
        repo_id: str,
        request: SkillRepositoryRequest,
    ) -> SkillRepositoryRecord:
        stored = self.get_repository_by_repo_id(repo_id)
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
            source_type=source_type,
            branch=branch or None,
            url=url or None,
            local_path=local_path or None,
        )

    def _ensure_path_under_archives_dir(self, target: Path) -> Path:
        resolved = target.expanduser().resolve()
        base = self._skill_archives_dir.resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            raise ValueError(
                f"Path {resolved} is outside allowed directory {base}"
            ) from None
        return resolved

    def delete_skill_repository(self, repo_id: str) -> None:
        stored = self.get_repository_by_repo_id(repo_id)
        if stored.local_path:
            local_path = Path(stored.local_path)
            try:
                local_path = self._ensure_path_under_archives_dir(local_path)
            except ValueError as exc:
                _logger.warning(f"Skipping local_path cleanup: {exc}")
            else:
                if local_path.is_file():
                    try:
                        local_path.unlink()
                    except Exception as exc:
                        _logger.warning(
                            f"Failed to clean up archive file {local_path}: {exc}"
                        )
                    extract_dir = self._resolve_extract_dir(stored)
                    try:
                        extract_dir = self._ensure_path_under_archives_dir(extract_dir)
                    except ValueError as exc:
                        _logger.warning(f"Skipping extract_dir cleanup: {exc}")
                    if extract_dir.exists():
                        try:
                            shutil.rmtree(extract_dir)
                        except Exception as exc:
                            _logger.warning(
                                f"Failed to clean up extract directory {extract_dir}: {exc}"
                            )
                elif local_path.is_dir():
                    try:
                        shutil.rmtree(local_path)
                    except Exception as exc:
                        _logger.warning(
                            f"Failed to clean up directory {local_path}: {exc}"
                        )
        self.repository.delete_skill_repository(repo_id)

    def create_skill_repository_from_archive(
        self,
        file: UploadFile,
    ) -> SkillRepositoryRecord:
        repository_name = file.filename
        if not repository_name.endswith(".zip"):
            raise ValueError("Only ZIP files are supported for upload")
        existing_repo = self.repository.get_skill_repository_by_name(repository_name)
        if existing_repo is not None:
            raise ValueError(
                f'Skill repository "{repository_name}" already exists with repo_id "{existing_repo.repo_id}"'
            )

        content = file.file.read()
        if len(content) > ZIP_MAX_ARCHIVE_SIZE:
            raise ValueError(
                f"Archive size {len(content)} bytes exceeds limit {ZIP_MAX_ARCHIVE_SIZE} bytes"
            )

        precheck_extract_dir = (
            self._skill_archives_dir / f"precheck-{Path(repository_name).stem}"
        )
        try:
            with ZipFile(BytesIO(content)) as archive:
                self._validate_zip_entries(archive, precheck_extract_dir)
        except BadZipFile as exc:
            raise ValueError("Invalid ZIP file") from exc

        archive_path = self._skill_archives_dir / repository_name
        archive_path.write_bytes(content)

        return self.repository.create_skill_repository(
            name=repository_name,
            source_type=SkillSourceType.LOCAL,
            branch=None,
            url=None,
            local_path=str(archive_path),
            skill_discover_status=SkillDiscoverStatus.INIT,
        )

    def get_repository_by_repo_id(self, repo_id: str) -> SkillRepositoryRecord:
        repository = self.repository.get_skill_repository(repo_id)
        if repository is None:
            raise KeyError(f"Skill repository {repo_id} not found")
        return repository

    def discover_skill_repositories(self) -> list[SkillRepositoryRecord]:
        updated_repositories: list[SkillRepositoryRecord] = []
        for repository in self.repository.list_skill_repositories():
            if is_awesome_openclaw_repository(repository):
                try:
                    updated_repositories.append(
                        sync_awesome_openclaw_skills(
                            repository=self.repository,
                            repo_id=repository.repo_id,
                        )
                    )
                except Exception as exc:
                    _logger.warning(
                        "Failed to discover awesome-openclaw-skills repository %s: %s",
                        repository.repo_id,
                        exc,
                    )
                    self.repository.update_skills(repository.repo_id, skills=[])
                    updated_repositories.append(
                        self.repository.update_skill_repository(
                            repository.repo_id,
                            skill_discover_status=SkillDiscoverStatus.FAILED,
                            skill_num=0,
                        )
                    )
                continue
            self._set_discovery_status(repository, SkillDiscoverStatus.DISCOVERING)
            try:
                skill_list = self._discover_skill_repository_skills(repository)
                self.repository.update_skills(repository.repo_id, skills=skill_list)
                updated_repositories.append(
                    self.repository.update_skill_repository(
                        repository.repo_id,
                        skill_discover_status=SkillDiscoverStatus.DONE,
                        skill_num=len(skill_list),
                    )
                )
            except Exception as exc:
                _logger.warning(
                    "Failed to discover skills from repository %s (%s): %s",
                    repository.repo_id,
                    repository.repo_name,
                    exc,
                )
                self.repository.update_skills(repository.repo_id, skills=[])
                updated_repositories.append(
                    self.repository.update_skill_repository(
                        repository.repo_id,
                        skill_discover_status=SkillDiscoverStatus.FAILED,
                        skill_num=0,
                    )
                )
        return updated_repositories

    def discover_one_skill_repository(self, repo_id: str) -> SkillRepositoryRecord:
        repository = self.get_repository_by_repo_id(repo_id)
        if repository.skill_discover_status == SkillDiscoverStatus.DISCOVERING:
            raise ValueError("Skill repository discovery is already in progress")

        if is_awesome_openclaw_repository(repository):
            try:
                return sync_awesome_openclaw_skills(
                    repository=self.repository,
                    repo_id=repository.repo_id,
                )
            except Exception as exc:
                _logger.warning(
                    "Failed to discover awesome-openclaw-skills repository %s: %s",
                    repository.repo_id,
                    exc,
                )
                self.repository.update_skills(repository.repo_id, skills=[])
                self.repository.update_skill_repository(
                    repository.repo_id,
                    skill_discover_status=SkillDiscoverStatus.FAILED,
                    skill_num=0,
                )
                raise ValueError(
                    f"Failed to discover awesome-openclaw-skills repository {repository.repo_id}"
                ) from exc

        self._set_discovery_status(repository, SkillDiscoverStatus.DISCOVERING)
        try:
            skill_list = self._discover_skill_repository_skills(repository)
            self.repository.update_skills(repository.repo_id, skills=skill_list)
            return self.repository.update_skill_repository(
                repository.repo_id,
                skill_discover_status=SkillDiscoverStatus.DONE,
                skill_num=len(skill_list),
            )
        except Exception as exc:
            _logger.warning(
                "Failed to discover skills from repository %s (%s): %s",
                repository.repo_id,
                repository.repo_name,
                exc,
            )
            self.repository.update_skills(repository.repo_id, skills=[])
            self.repository.update_skill_repository(
                repository.repo_id,
                skill_discover_status=SkillDiscoverStatus.FAILED,
                skill_num=0,
            )
            raise ValueError(
                f"Failed to discover skills from repository {repository.repo_id}"
            ) from exc

    def get_skill_by_skill_id(self, skill_id: str) -> SkillRecord | None:
        return self.repository.get_skill_by_skill_id(skill_id)

    def get_skill_source_path(self, skill: SkillRecord) -> str | None:
        """
        四种技能来源：
        1. buildin的skill_source_path是绝对路径：直接返回relative_path.parent
        2. git/local是本地路径：根据repo_id和relative_path拼接
        3. clawhub的技能，没有relative_path：返回None
        """
        # 如果relative_path为空，返回None, 比如clawhub的技能
        if skill.relative_path is None:
            return None

        # 如果是绝对路径，直接返回， 比如openclaw技能的绝对路径
        if os.path.isabs(skill.relative_path):
            return str(Path(skill.relative_path).parent)

        if skill.repo_id is None:
            return None
        repo = self.repository.get_skill_repository(skill.repo_id)
        if repo is None or not repo.local_path:
            return None
        local_path = Path(str(repo.local_path)).expanduser().resolve(strict=False)
        if local_path.is_file() and local_path.suffix == ".zip":
            extract_dir = self._resolve_extract_dir(repo)
            if not extract_dir.exists():
                return None
            repo_root = self._find_archive_repo_root(extract_dir)
        else:
            repo_root = local_path
        skill_file = repo_root / skill.relative_path
        skill_dir = skill_file.parent
        if not skill_dir.exists():
            return None
        return str(skill_dir)

    def list_skills(self) -> list[SkillRecord]:
        return self.repository.list_skills()

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
                "Background discover failed for repository %s: %s", repo_id, exc
            )

    @classmethod
    def sync_awesome_repository_in_background(
        cls,
        *,
        repository: SqliteRepository,
    ) -> None:
        try:
            sync_awesome_openclaw_skills(repository=repository, repo_id=None)
        except Exception as exc:
            _logger.warning("Background awesome-openclaw-skills sync failed: %s", exc)
            awesome_repo = repository.get_skill_repository_by_name(AWESOME_REPO_URL)
            if awesome_repo is not None:
                repository.update_skill_repository(
                    awesome_repo.repo_id,
                    skill_discover_status=SkillDiscoverStatus.FAILED,
                    skill_num=0,
                )

    def _set_discovery_status(self, repo: SkillRepositoryRecord, status: str) -> None:
        self.repository.update_skill_repository(
            repo.repo_id,
            skill_discover_status=status,
            skill_num=repo.skill_num,
        )

    def _normalize_create_request(
        self, request: SkillRepositoryRequest
    ) -> SkillRepositoryRequest:
        if request.source_type is None:
            raise ValueError("source_type is required")

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
            return ""
        stripped = url.strip()

        ssh_match = re.match(r"git@([^:]+):(.+)", stripped)
        if ssh_match:
            host = ssh_match.group(1)
            path = ssh_match.group(2).strip("/")
            return f"git@{host}:{path}"

        parsed = urlparse(stripped)
        if not parsed.scheme or not parsed.netloc:
            return ""

        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"{parsed.scheme}://{parsed.netloc}/{path}"

    def _derive_git_repository_name(self, request: SkillRepositoryRequest) -> str:
        if request.source_type != SkillSourceType.GIT:
            raise ValueError(f"Invalid source_type: {request.source_type}")

        if not request.url:
            raise ValueError("git skill repositories require url")
        normalized_url = request.url.removesuffix(".git")
        if request.branch is None:
            return normalized_url
        return f"{normalized_url}@{request.branch}"

    def _validate_source_fields(
        self,
        *,
        source_type: str,
        url: str | None,
        local_path: str | None,
    ) -> None:
        if source_type == SkillSourceType.GIT:
            if not url:
                raise ValueError("git skill repositories require url")
            return
        if source_type == SkillSourceType.LOCAL:
            if not local_path:
                raise ValueError("local skill repositories require local_path")
            return
        raise ValueError(f"Unsupported skill repository source type: {source_type}")

    def _discover_skill_repository_skills(
        self, repo: SkillRepositoryRecord
    ) -> list[SkillRecord]:
        if repo.source_type == SkillSourceType.LOCAL:
            return self._discover_local_skill_repository_skills(repo)
        return self._discover_git_skill_repository_skills(repo)

    def _discover_git_skill_repository_skills(
        self, repo: SkillRepositoryRecord
    ) -> list[SkillRecord]:
        clone_url = self._normalize_clone_url_for_git(repo.url)
        name = repo.repo_name.split("@")[0].split("/")[-1]
        clone_dir = self._skill_archives_dir / f"{name}-{repo.repo_id}"
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
        clone_dir.mkdir(parents=True, exist_ok=True)
        command = ["git", "clone", "--depth", "1"]
        if repo.branch:
            command.extend(["--branch", repo.branch])
        command.extend([clone_url, str(clone_dir)])
        last_exc: subprocess.CalledProcessError | None = None
        for _ in range(GIT_CLONE_RETRY_TIMES):
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
                break
            except subprocess.CalledProcessError as exc:
                last_exc = exc
        else:
            if last_exc is None:
                raise RuntimeError(
                    "command loop failed without exception"
                )  # pragma: no cover
            raise last_exc

        if repo.branch is None:
            detected_branch = self._get_cloned_repo_branch(clone_dir)
            if detected_branch:
                repo = self.repository.update_skill_repository(
                    repo.repo_id,
                    branch=detected_branch,
                )
        repo = self.repository.update_skill_repository(
            repo.repo_id,
            local_path=str(clone_dir),
        )
        return self._scan_skill_repository_root(
            repo=repo,
            repo_root=clone_dir,
            only_root=False,
        )

    def _get_cloned_repo_branch(self, clone_dir: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(clone_dir), "rev-parse", "--abbrev-ref", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            return None
        branch = result.stdout.strip()
        if not branch or branch == "HEAD":
            return None
        return branch

    def _normalize_clone_url_for_git(self, repo_url: str | None) -> str:
        if not repo_url:
            raise ValueError("git skill repositories require url")
        if repo_url.endswith(".git"):
            return repo_url
        return f"{repo_url}.git"

    def _discover_local_skill_repository_skills(
        self, repo: SkillRepositoryRecord
    ) -> list[SkillRecord]:
        local_path = Path(str(repo.local_path)).expanduser().resolve(strict=False)
        extract_dir = self._prepare_archive_extract_dir(repo)
        repo_root = self._extract_local_archive_to_dir(local_path, extract_dir)
        return self._scan_local_skill_repository_root(repo, repo_root)

    def _resolve_extract_dir(self, repo: SkillRepositoryRecord) -> Path:
        local_path = Path(str(repo.local_path)).expanduser().resolve(strict=False)
        stem = local_path.stem if local_path.suffix == ".zip" else local_path.name
        return self._skill_archives_dir / f"{stem}-{repo.repo_id}"

    def _prepare_archive_extract_dir(self, repo: SkillRepositoryRecord) -> Path:
        extract_dir = self._resolve_extract_dir(repo)
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        return extract_dir

    @staticmethod
    def _validate_zip_entries(archive: ZipFile, extract_dir: Path) -> None:
        extract_dir_resolved = extract_dir.resolve()
        members = archive.infolist()
        if len(members) > ZIP_MAX_ENTRY_COUNT:
            raise ValueError(
                f"ZIP contains {len(members)} entries, exceeds limit {ZIP_MAX_ENTRY_COUNT}"
            )
        total_size = 0
        for member in members:
            total_size += member.file_size
            if total_size > ZIP_MAX_EXTRACTED_SIZE:
                raise ValueError(
                    f"ZIP extracted size {total_size} bytes exceeds limit {ZIP_MAX_EXTRACTED_SIZE} bytes"
                )
            if stat.S_IFMT(member.external_attr >> 16) == stat.S_IFLNK:
                # external_attr 检测 S_IFLNK ，拒绝符号链接
                raise ValueError(
                    f"ZIP entry '{member.filename}' is a symbolic link, which is not allowed"
                )
            parts = member.filename.split(os.path.sep)
            # 检查 ’..‘，与 resolve().relative_to() 互补， 双重防护，防止路径穿越
            if ".." in parts:
                raise ValueError(
                    f"ZIP entry '{member.filename}' contains path traversal sequence"
                )
            member_path = (extract_dir_resolved / member.filename).resolve()
            try:
                member_path.relative_to(extract_dir_resolved)
            except ValueError:
                raise ValueError(
                    f"ZIP entry '{member.filename}' resolves to path outside target directory"
                ) from None

    def _extract_local_archive_to_dir(
        self,
        archive_path: Path,
        extract_dir: Path,
    ) -> Path:
        with ZipFile(archive_path) as archive:
            self._validate_zip_entries(archive, extract_dir)
            archive.extractall(extract_dir)
        return self._find_archive_repo_root(extract_dir)

    def _find_archive_repo_root(self, extract_dir: Path) -> Path:
        children = [child for child in extract_dir.iterdir() if child.is_dir()]
        if len(children) == 1:
            return children[0]
        return extract_dir

    def _scan_local_skill_repository_root(
        self, repo: SkillRepositoryRecord, repo_root: Path
    ) -> list[SkillRecord]:
        root_skill = repo_root / "SKILL.md"
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
    ) -> list[SkillRecord]:
        if not repo_root.exists():
            raise ValueError(
                f"Repository root does not exist for repo_id {repo.repo_id}: {repo_root}"
            )

        if only_root:
            skill_files = [repo_root / "SKILL.md"]
        else:
            skill_files = sorted(
                path
                for path in repo_root.rglob("*")
                if path.is_file() and path.name == "SKILL.md"
            )

        discovered: list[SkillRecord] = []
        for skill_file in skill_files:
            metadata, _ = self._load_skill_frontmatter(skill_file)
            relative_path = self._to_repository_relative_path(repo_root, skill_file)
            skill_source = (
                repo.local_path
                if repo.source_type == SkillSourceType.LOCAL
                else repo.url
            )
            skill_md_url = self._build_skill_md_url(repo, relative_path)
            skill_id = self._build_deterministic_skill_id(repo.repo_id, relative_path)
            discovered.append(
                SkillRecord(
                    skill_id=skill_id,
                    repo_id=repo.repo_id,
                    skill_name=self._derive_repository_skill_name(skill_file, metadata),
                    relative_path=relative_path,
                    metadata=metadata,
                    skill_source=skill_source,
                    skill_md_url=skill_md_url,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        return discovered

    def _build_deterministic_skill_id(self, repo_id: str, relative_path: str) -> str:
        unique_key = f"{repo_id}:{relative_path}"
        return str(uuid5(NAMESPACE_URL, unique_key))

    def _load_skill_frontmatter(
        self, skill_file: Path
    ) -> tuple[dict[str, object], str]:
        text = skill_file.read_text(encoding="utf-8")
        stripped = text.lstrip()
        if not stripped.startswith("---"):
            return {}, text.strip()
        parts = stripped.split("---", maxsplit=2)
        if len(parts) < 3:
            return {}, text.strip()

        raw_frontmatter = parts[1]
        content = parts[2].lstrip("\r\n")

        metadata: dict[str, object] = {}
        current_key: str | None = None
        for line in raw_frontmatter.splitlines():
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith("#"):
                continue

            if stripped_line.startswith("- ") and current_key == "triggers":
                triggers = metadata.setdefault("triggers", [])
                if isinstance(triggers, list):
                    trigger = stripped_line[2:].strip()
                    if trigger:
                        triggers.append(trigger)
                continue

            if ":" not in line:
                if current_key is not None:
                    existing = metadata.get(current_key)
                    if isinstance(existing, str):
                        metadata[current_key] = f"{existing} {stripped_line}".strip()
                continue

            key, value = line.split(":", 1)
            current_key = key.strip()
            metadata[current_key] = self._parse_frontmatter_value(
                current_key, value.strip()
            )

        return metadata, content.strip()

    def _parse_frontmatter_value(self, key: str, value: str) -> object:
        if not value:
            return [] if key == "triggers" else ""

        if key == "triggers":
            if value.startswith("[") and value.endswith("]"):
                try:
                    parsed = json.loads(value.replace("'", '"'))
                except Exception:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            return [value.strip("\"'")] if value.strip("\"'") else []

        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return value.strip("\"'")

    def _derive_repository_skill_name(
        self, skill_file: Path, metadata: dict[str, object]
    ) -> str:
        metadata_name = metadata.get("name")
        if isinstance(metadata_name, str) and metadata_name.strip():
            return metadata_name.strip()
        if skill_file.name == "SKILL.md":
            return skill_file.parent.name
        return skill_file.stem

    def _to_repository_relative_path(self, repo_root: Path, skill_file: Path) -> str:
        return skill_file.relative_to(repo_root).as_posix()

    def _build_skill_md_url(
        self,
        repo: SkillRepositoryRecord,
        relative_path: str,
    ) -> str | None:
        if repo.source_type == SkillSourceType.LOCAL:
            return relative_path

        if not repo.url:
            return None
        browse_base_url = self._normalize_repository_browse_base_url(repo.url)
        if not browse_base_url:
            return None
        branch = repo.branch or "HEAD"
        cleaned_relative_path = relative_path.lstrip("/")
        return f"{browse_base_url}/blob/{branch}/{cleaned_relative_path}"

    def _normalize_repository_browse_base_url(self, repo_url: str) -> str | None:
        normalized_url = repo_url.strip()
        if not normalized_url:
            return None

        ssh_match = re.match(r"git@([^:]+):(.+)", normalized_url)
        if ssh_match:
            host = ssh_match.group(1)
            path = ssh_match.group(2).strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return f"https://{host}/{path}"

        parsed = urlparse(normalized_url)
        if not parsed.netloc:
            return None
        path = parsed.path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        if not path:
            return None
        return f"{parsed.scheme}://{parsed.netloc}/{path}"

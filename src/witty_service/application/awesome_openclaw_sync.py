from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

from witty_service.persistence.repositories import (
    SkillRecord,
    SkillRepositoryRecord,
    SqliteRepository,
)

AWESOME_REPO_URL = "https://github.com/VoltAgent/awesome-openclaw-skills"
AWESOME_REPO_NAME = "awesome-openclaw-skills"
AWESOME_REPO_BRANCH = "main"
AWESOME_SOURCE_TYPE = "clawhub"
AWESOME_CATEGORIES_DIR = "categories"
AWESOME_DISCOVER_STATUS_DONE = "done"
AWESOME_DISCOVER_STATUS_DISCOVERING = "discovering"
_CLONE_TIMEOUT_SECONDS = 120

_SKILL_LINE_PATTERN = re.compile(
    r"^\s*-\s*\[([^\]]+)\]\((https?://[^)]+)\)\s*-\s*(.+?)\s*$"
)


@dataclass(slots=True)
class ParsedSkill:
    skill_name: str
    skill_md_url: str
    description: str
    category: str


def is_awesome_openclaw_repository(repository: SkillRepositoryRecord) -> bool:
    normalized_url = _normalize_repo_url(repository.url)
    return normalized_url == AWESOME_REPO_URL


def sync_awesome_openclaw_skills(
    *,
    repository: SqliteRepository,
    repo_id: str | None = None,
) -> SkillRepositoryRecord:
    target_repo = _resolve_or_create_repository(repository=repository, repo_id=repo_id)
    if (
        target_repo.skill_discover_status == AWESOME_DISCOVER_STATUS_DONE
        and repo_id is None
    ):
        return target_repo

    repository.update_skill_repository(
        target_repo.repo_id,
        source_type=AWESOME_SOURCE_TYPE,
        branch=AWESOME_REPO_BRANCH,
        url=AWESOME_REPO_URL,
        skill_discover_status=AWESOME_DISCOVER_STATUS_DISCOVERING,
    )

    with TemporaryDirectory(prefix="awesome-openclaw-skills-") as temp_dir:
        checkout_dir = _clone_repo(Path(temp_dir))
        skill_records = _build_skill_records(target_repo.repo_id, checkout_dir)

    repository.update_skills(target_repo.repo_id, skills=skill_records)
    return repository.update_skill_repository(
        target_repo.repo_id,
        source_type=AWESOME_SOURCE_TYPE,
        branch=AWESOME_REPO_BRANCH,
        url=AWESOME_REPO_URL,
        skill_discover_status=AWESOME_DISCOVER_STATUS_DONE,
        skill_num=len(skill_records),
    )


def _resolve_or_create_repository(
    *, repository: SqliteRepository, repo_id: str | None
) -> SkillRepositoryRecord:
    if repo_id is not None:
        existing = repository.get_skill_repository(repo_id)
        if existing is None:
            raise KeyError(f"Skill repository not found: {repo_id}")
        if not is_awesome_openclaw_repository(existing):
            raise ValueError(
                f"Repository {repo_id} is not {AWESOME_REPO_URL}: "
                f"name={existing.repo_name} url={existing.url}"
            )
        return existing

    by_name = repository.get_skill_repository_by_name(AWESOME_REPO_URL)
    if by_name is not None:
        return by_name

    return repository.create_skill_repository(
        name=AWESOME_REPO_URL,
        source_type=AWESOME_SOURCE_TYPE,
        branch=AWESOME_REPO_BRANCH,
        url=AWESOME_REPO_URL,
        local_path=None,
        skill_discover_status=AWESOME_DISCOVER_STATUS_DISCOVERING,
    )


def _normalize_repo_url(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.strip()
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.rstrip("/")


def _clone_repo(target_dir: Path) -> Path:
    checkout_dir = target_dir / AWESOME_REPO_NAME
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            AWESOME_REPO_BRANCH,
            AWESOME_REPO_URL,
            str(checkout_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=_CLONE_TIMEOUT_SECONDS,
    )
    return checkout_dir


def _build_skill_records(repo_id: str, checkout_dir: Path) -> list[SkillRecord]:
    categories_dir = checkout_dir / AWESOME_CATEGORIES_DIR
    if not categories_dir.exists():
        raise FileNotFoundError(f"Missing categories directory: {categories_dir}")

    now = datetime.now(UTC)
    records: list[SkillRecord] = []
    for category_file in sorted(categories_dir.glob("*.md")):
        category_name = category_file.stem
        for parsed in _parse_category_file(category_file, category_name):
            unique_key = (
                f"{repo_id}:{category_name}:{parsed.skill_name}:{parsed.skill_md_url}"
            )
            records.append(
                SkillRecord(
                    skill_id=str(uuid5(NAMESPACE_URL, unique_key)),
                    repo_id=repo_id,
                    skill_name=parsed.skill_name,
                    relative_path=None,
                    metadata={
                        "name": parsed.skill_name,
                        "description": parsed.description,
                        "category": parsed.category,
                    },
                    skill_source=AWESOME_REPO_URL,
                    skill_md_url=parsed.skill_md_url,
                    created_at=now,
                    updated_at=now,
                )
            )
    return records


def _parse_category_file(category_file: Path, category_name: str) -> list[ParsedSkill]:
    parsed: list[ParsedSkill] = []
    for raw_line in category_file.read_text(encoding="utf-8").splitlines():
        parsed_item = _parse_skill_line(raw_line, category_name)
        if parsed_item is None:
            continue
        parsed.append(parsed_item)
    return parsed


def _parse_skill_line(line: str, category_name: str) -> ParsedSkill | None:
    match = _SKILL_LINE_PATTERN.match(line)
    if match is None:
        return None

    skill_name = match.group(1).strip()
    skill_md_url = match.group(2).strip()
    tail = match.group(3).strip()
    description = _extract_description(skill_name, tail)
    if not skill_name or not description:
        return None
    return ParsedSkill(
        skill_name=skill_name,
        skill_md_url=skill_md_url,
        description=description,
        category=category_name,
    )


def _extract_description(skill_name: str, tail: str) -> str:
    normalized_name = skill_name.strip().lower()
    for separator in (" — ", " - ", " – "):
        if separator not in tail:
            continue
        head, rest = tail.split(separator, maxsplit=1)
        if head.strip().lower() == normalized_name:
            return rest.strip()
    return tail.strip()

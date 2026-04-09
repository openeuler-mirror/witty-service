from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from openhands.agent_server.utils import utc_now


class SkillRepoSourceType(str, Enum):
    GIT = 'git'
    LOCAL_IMPORT = 'local_import'


class SkillRepo(BaseModel):
    repo_id: str
    name: str = Field(min_length=1, max_length=255)
    source_type: SkillRepoSourceType
    branch: str | None = None
    url: str | None = None
    local_path: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillRepoPage(BaseModel):
    items: list[SkillRepo]


class CreateSkillRepoRequest(BaseModel):
    source_type: SkillRepoSourceType
    branch: str | None = None
    url: str | None = None
    local_path: str | None = None


class UpdateSkillRepoRequest(BaseModel):
    source_type: SkillRepoSourceType | None = None
    branch: str | None = None
    url: str | None = None
    local_path: str | None = None

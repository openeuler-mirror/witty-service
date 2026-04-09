from enum import Enum

from pydantic import BaseModel, Field

from openhands.app_server.skills.skill_repo.skill_repo_models import SkillRepoSourceType


class SkillDiscoveryActivationType(str, Enum):
    ALWAYS = 'always'
    TRIGGERED = 'triggered'
    TASK = 'task'


class SkillSourceRepo(BaseModel):
    repo_id: str
    name: str
    source_type: SkillRepoSourceType
    branch: str | None = None
    url: str | None = None
    local_path: str | None = None


class SkillDiscoveryItem(BaseModel):
    key: str
    name: str
    activation_type: SkillDiscoveryActivationType
    triggers: list[str] = Field(default_factory=list)
    origin_path: str | None = None
    content: str | None = None
    source_repo: SkillSourceRepo | None = None
    source_ref: str | None = None
    readme_url: str | None = None

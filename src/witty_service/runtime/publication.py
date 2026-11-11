from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from witty_service.runtime.projection import RuntimeProjection


class PublicationPhase(StrEnum):
    IDLE = "idle"
    PUBLISHING = "publishing"
    DRAINING = "draining"
    RESTARTING = "restarting"
    VERIFYING = "verifying"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkspacePublicationState:
    """发布是运行时事实：只存在内存，不生成配置版本或候选配置。"""

    workspace_id: str
    phase: PublicationPhase
    published_projection_hash: str | None = None
    pending_projection_hash: str | None = None
    active_turns: int = 0
    last_error: str | None = None


class RuntimeProjectionSource(Protocol):
    """按 Workspace 生成全量确定性投影；发布开始时只取一次并冻结。"""

    async def load(self, workspace_id: str) -> RuntimeProjection: ...


class RuntimeProjectionStore(Protocol):
    """``published_projection_hash`` 的唯一持久化边界，由 Workspace 聚合实现。"""

    async def get_published_hash(self, workspace_id: str) -> str | None: ...

    async def save_published_hash(
        self, workspace_id: str, projection_hash: str
    ) -> None: ...


class RuntimeProjectionRenderer(Protocol):
    """把投影渲染成 Runtime Home 内相对路径到内容的映射；不写文件、不碰进程。"""

    def render(
        self, projection: RuntimeProjection, credentials: Mapping[str, str]
    ) -> Mapping[str, str]: ...


class RuntimeCredentialResolver(Protocol):
    """发布时解析 provider 凭据；凭据本体不进入投影 hash 与发布状态。"""

    async def resolve(self, provider_ids: tuple[str, ...]) -> Mapping[str, str]: ...

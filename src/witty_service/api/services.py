from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from witty_service.adapter.http_client import AdaptorHttpClient
from witty_service.adapter.websocket_client_pool import WebSocketClientPool
from witty_service.application.agent_manager import AGENT_NOT_FOUND, AgentManager
from witty_service.application.scheduled_task_service import ScheduledTaskService
from witty_service.application.session_manager import SessionManager
from witty_service.channels.credential_store import ChannelCredentialStore
from witty_service.channels.dedup import DEFAULT_RETENTION_DAYS, InboundDedup
from witty_service.channels.gateway import ChannelGateway
from witty_service.channels.provisioning.flow import ProvisioningFlow
from witty_service.channels.provisioning.manual import ManualCredentialBinder
from witty_service.channels.router import SessionRouter
from witty_service.channels.turn_gateway import AgentTurnGateway
from witty_service.config import get_settings
from witty_service.domain.errors import DomainError, insight_disabled
from witty_service.persistence.channel_repository import (
    ChannelInstanceRecord,
    ChannelRepository,
)
from witty_service.persistence.db import (
    create_session_factory,
    create_sqlite_engine,
    init_db,
)
from witty_service.persistence.repositories import SqliteRepository
from witty_service.sandbox.base import SandboxBackend
from witty_service.sandbox.factory import create_sandbox_backend
from witty_service.storage.workspace_store import LocalWorkspaceStore, WorkspaceStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ServiceContainer:
    repository: SqliteRepository
    workspace_store: WorkspaceStore
    sandbox_backends: dict[str, SandboxBackend] = field(default_factory=dict)
    ws_client_pool: WebSocketClientPool = field(default_factory=WebSocketClientPool)
    insight_http_client: AdaptorHttpClient | None = None
    session_manager: SessionManager = field(init=False)
    insight_facade: Any = field(init=False, default=None)
    scheduled_task_service: ScheduledTaskService = field(init=False)
    # --- IM Channel 渠道层 ---------------------------
    # 这些成员在 __post_init__ 里**只做装配、不做 IO**：ServiceContainer 会被大量
    # 测试以 MagicMock() 依赖反复构造，任何 IO（含建凭据目录）都会让无关测试连带
    # 失败。凭据目录的创建与校验都推迟到 ChannelGateway.start()。
    channel_repository: ChannelRepository = field(init=False)
    channel_dedup: InboundDedup = field(init=False)
    channel_turn_gateway: AgentTurnGateway = field(init=False)
    channel_router: SessionRouter = field(init=False)
    channel_gateway: ChannelGateway = field(init=False)
    channel_provisioning: ProvisioningFlow | None = field(init=False, default=None)
    channel_manual_binder: ManualCredentialBinder | None = field(
        init=False, default=None
    )

    def __post_init__(self) -> None:
        self.session_manager = SessionManager(repository=self.repository)
        self.scheduled_task_service = ScheduledTaskService(
            repository=self.repository,
            get_agent_manager=self.get_agent_manager_for_agent,
            settings=get_settings().scheduler,
        )
        self._build_channel_components()

    # ==========================================================================
    # IM Channel 渠道层装配
    # ==========================================================================

    def _build_channel_components(self) -> None:
        channel_settings = get_settings().channel
        self.channel_repository = ChannelRepository(self.repository.session_factory)
        self.channel_dedup = InboundDedup(
            self.channel_repository,
            retention_days=(
                channel_settings.inbound_retention_days or DEFAULT_RETENTION_DAYS
            ),
        )
        # 渠道标识符按**实例**取值：SessionRouter 调用时传入，因此一个回合网关
        # 就能服务多个渠道实例（会话来源标记取实际实例的渠道）。
        self.channel_turn_gateway = AgentTurnGateway(
            repository=self.repository,
            get_agent_manager=self.get_agent_manager_for_agent,
            channel="",
        )
        self.channel_router = SessionRouter(
            repository=self.channel_repository,
            gateway=self.channel_turn_gateway,
            dedup=self.channel_dedup,
            queue_depth=channel_settings.queue_depth,
            stall_window_seconds=channel_settings.stall_window_seconds,
            edit_throttle_ms=channel_settings.edit_throttle_ms,
        )
        self.channel_gateway = ChannelGateway(
            repository=self.channel_repository,
            router=self.channel_router,
            settings=channel_settings,
            dedup=self.channel_dedup,
        )

    def get_channel_credentials(self) -> ChannelCredentialStore:
        """渠道凭据存储。只构造一个路径对象"""
        return ChannelCredentialStore.from_settings(get_settings().channel)

    def get_channel_provisioning(self) -> ProvisioningFlow:
        """扫码接入编排（进程内单例：同一实例的进行中尝试与节流都记在内存里）。"""
        if self.channel_provisioning is None:
            self.channel_provisioning = ProvisioningFlow(
                repository=self.channel_repository,
                store=self.get_channel_credentials(),
                on_instance_ready=self.notify_channel_instance_ready,
            )
        return self.channel_provisioning

    def get_channel_manual_binder(self) -> ManualCredentialBinder:
        """手填凭据旁路（与扫码共用同一落库路径）。"""
        if self.channel_manual_binder is None:
            self.channel_manual_binder = ManualCredentialBinder(
                repository=self.channel_repository,
                store=self.get_channel_credentials(),
                on_instance_ready=self.notify_channel_instance_ready,
            )
        return self.channel_manual_binder

    async def notify_channel_instance_ready(
        self, record: ChannelInstanceRecord
    ) -> None:
        """接入成功后的装配钩子：网关未运行时只落库，由重连接口或重启再连。"""
        if self.channel_gateway.running:
            await self.channel_gateway.connect_instance(record.id)

    def get_sandbox_backend(self, sandbox_type: str) -> SandboxBackend:
        key = sandbox_type.lower()
        backend = self.sandbox_backends.get(key)
        if backend is None:
            backend = create_sandbox_backend(key)
            self.sandbox_backends[key] = backend
        return backend

    def get_agent_manager_for_sandbox(self, sandbox_type: str) -> AgentManager:
        return AgentManager(
            repository=self.repository,
            session_manager=self.session_manager,
            workspace_store=self.workspace_store,
            sandbox_backend=self.get_sandbox_backend(sandbox_type),
            ws_client_pool=self.ws_client_pool,
        )

    def get_agent_manager_for_agent(self, agent_id: str) -> AgentManager:
        agent = self.repository.get_agent(agent_id)
        if agent is None:
            raise DomainError(
                code=AGENT_NOT_FOUND,
                message="Agent was not found.",
                details={"agent_id": agent_id},
            )
        return self.get_agent_manager_for_sandbox(agent.sandbox_type)

    def get_insight_http_client(self) -> AdaptorHttpClient:
        if self.insight_http_client is None:
            raise insight_disabled()
        return self.insight_http_client

    def get_insight_facade(self) -> Any:
        if self.insight_facade is None:
            from witty_service.application.insight_facade import InsightFacade

            self.insight_facade = InsightFacade(self)
        return self.insight_facade

    async def close(self) -> None:
        self._stop_sandbox_backends()
        if self.insight_http_client is not None:
            await self.insight_http_client.close()

    def _stop_sandbox_backends(self) -> None:
        """关停时停止本进程拉起的沙箱（目前只有 local_process 需要）。

        漏掉这一步会让沙箱子进程（以及它们拉起的 opencode serve）变成孤儿：每重启
        一次就多占一份端口和内存，后续恢复会因为端口被占而失败。docker 容器的
        stop_all 是刻意的空实现，原因见 DockerSandboxBackend.stop_all。

        同步执行：``close()`` 已经是关停路径，且在事件循环里调用阻塞式的进程收敛
        会造成「关停时事件循环忙等」。
        """
        if not get_settings().workspace.stop_sandboxes_on_shutdown:
            logger.info("Skip stopping sandboxes on shutdown (disabled by settings)")
            return
        for sandbox_type, backend in list(self.sandbox_backends.items()):
            try:
                backend.stop_all()
            except Exception:
                logger.exception(
                    "Failed to stop %s sandboxes on shutdown", sandbox_type
                )


def _ensure_dir_exists(database_url: str) -> None:
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)


def build_default_services() -> ServiceContainer:
    settings = get_settings()
    database_url = settings.database.url
    workspace_root = settings.workspace.root
    insight_settings = settings.insight

    _ensure_dir_exists(database_url)
    engine = create_sqlite_engine(database_url)
    init_db(engine, auto_create=settings.database.auto_create)

    insight_http_client = None
    if insight_settings.enabled:
        headers: dict[str, str] | None = None
        if insight_settings.bearer_token:
            headers = {"Authorization": f"Bearer {insight_settings.bearer_token}"}
        insight_http_client = AdaptorHttpClient(
            base_url=insight_settings.base_url,
            timeout=insight_settings.timeout_seconds,
            default_headers=headers,
        )

    return ServiceContainer(
        repository=SqliteRepository(create_session_factory(engine)),
        workspace_store=LocalWorkspaceStore(base_path=workspace_root),
        insight_http_client=insight_http_client,
    )

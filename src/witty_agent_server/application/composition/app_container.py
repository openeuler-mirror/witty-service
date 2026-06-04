from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter

from witty_agent_server.api.routers.agent_router import create_agent_router
from witty_agent_server.api.routers.session_router import create_session_router
from witty_agent_server.api.routers.session_ws_router import create_session_ws_router

from witty_agent_server.application.composition.runtime_instance_manager import (
    RuntimeInstanceManager,
)

from witty_agent_server.application.services.agent import (
    AgentService,
    OpenClawAgentService,
    OpenCodeAgentService,
)

from witty_agent_server.application.services.session_manage.base import (
    SessionStateEventPublisherBase,
    SessionTaskPoolBase,
    SessionTurnExecutorBase,
)
from witty_agent_server.application.services.session_manage.orchestrator import (
    SessionWSOrchestrator,
)
from witty_agent_server.application.services.session_manage.state_sync_service import (
    SessionStateSyncService,
)
from witty_agent_server.application.services.session_manage.task_pool import TaskPool

from witty_agent_server.application.services.session_to_runtime import (
    OpenClawSessionService,
    OpenCodeSessionService,
    SessionServiceBase,
    SessionService,
)
from witty_agent_server.application.services.session_to_runtime.runtime_registry import (
    RuntimeRegistry,
)

from witty_agent_server.infra.persistence.in_memory import InMemorySessionRepository
from witty_agent_server.runtimes.openclaw_runtime_factory import create_openclaw_runtime
from witty_agent_server.runtimes.runtime_base import RuntimeBase, RuntimeType


logger = logging.getLogger(__name__)


RuntimeFactory = Callable[[], RuntimeBase]


# ============================================================================
# 应用依赖容器
# ============================================================================


@dataclass(frozen=True)
class AppContainer:
    """应用级依赖容器，统一装配 router 所需依赖。

    AppContainer 是整个应用的核心依赖注入容器，负责:
    1. 创建和管理所有服务的依赖关系
    2. 提供统一的依赖访问接口
    3. 创建 FastAPI 路由器

    容器采用不可变设计 (frozen=True)，所有依赖在构建时确定，
    确保运行时依赖关系的稳定性。

    核心依赖组件:
    - binding_resolver: Agent 到 runtime 的绑定解析器
    - runtime_instance_manager: 运行时实例管理器
    - runtime_registry: 运行时注册表
    - runtime_factories: 运行时工厂映射
    - agent_service: Agent 服务门面
    - session_service: Session 服务门面
    - state_sync_service: 会话状态同步服务
    - session_ws_orchestrator: WebSocket 会话编排器
    - task_pool: 异步任务池
    """

    runtime_instance_manager: RuntimeInstanceManager
    runtime_registry: RuntimeRegistry | None
    runtime_factories: Mapping[RuntimeType, RuntimeFactory]
    agent_service: AgentService
    session_service: SessionService
    state_sync_service: SessionStateEventPublisherBase
    session_ws_orchestrator: SessionTurnExecutorBase
    task_pool: SessionTaskPoolBase

    @classmethod
    def build(
        cls,
        *,
        base_root: Path | str | None = None,
        agent_service: AgentService | None = None,
        session_service: SessionService | None = None,
        state_sync_service: SessionStateEventPublisherBase | None = None,
        runtime_factories: Mapping[RuntimeType, RuntimeFactory] | None = None,
        runtime_instance_manager: RuntimeInstanceManager | None = None,
    ) -> AppContainer:
        """构建容器并完成核心依赖装配。

        这是容器的主要构建方法，负责按正确的顺序创建和组装所有依赖组件。
        构建顺序至关重要，因为组件之间存在依赖关系。

        构建流程:
        ┌─────────────────────────────────────────────────────────────┐
        │ 1. RuntimeInstanceManager                                    │
        │    └→ 管理运行时实例目录、profile、port 等                    │
        │                                                               │
        │ 2. RuntimeFactories                                          │
        │    └→ 注册运行时工厂（默认 openclaw）                          │
        │                                                               │
        │ 3. AgentService                                              │
        │    └→ Agent 服务（启动/停止/状态管理）                         │
        │                                                               │
        │ 4. SessionService                                            │
        │    └→ Session 服务（会话生命周期管理）                         │
        │                                                               │
        │ 5. SessionStateSyncService                                   │
        │    └→ 会话状态同步服务                                        │
        │                                                               │
        │ 6. SessionWSOrchestrator                                     │
        │    └→ WebSocket 会话编排器                                    │
        │                                                               │
        │ 7. TaskPool                                                  │
        │    └→ 任务池（管理并发执行）                                   │
        └─────────────────────────────────────────────────────────────┘

        Args:
            base_root: 工作空间根目录，默认 ~/witty-service/
            agent_service: 可选的预构建 Agent 服务实例
            session_service: 可选的预构建 Session 服务实例
            state_sync_service: 可选的预构建状态同步服务实例
            runtime_factories: 可选的运行时工厂映射，默认注册 openclaw
            runtime_instance_manager: 可选的预构建运行时实例管理器实例

        Returns:
            完全初始化的 AppContainer 实例

        Example:
            >>> container = AppContainer.build(base_root="~/my-workspace")
            >>> app.include_router(container.create_agent_router())
        """

        # ----------------------------------------------------------------
        # 步骤 1: 创建绑定解析器
        # 决定每个 agent 应该使用哪个 runtime (openclaw/opencode)
        # ----------------------------------------------------------------
        # 步骤 1: 创建运行时实例管理器
        # 负责管理 agent 的实例目录、profile、port 等
        # ----------------------------------------------------------------
        resolved_runtime_instance_manager = (
            runtime_instance_manager
            or RuntimeInstanceManager(base_root=base_root or "~/witty-service/")
        )

        # ----------------------------------------------------------------
        # 步骤 3: 注册运行时工厂
        # 工厂负责创建具体的 runtime 实例
        # ----------------------------------------------------------------
        resolved_runtime_factories = dict(
            runtime_factories or {"openclaw": create_openclaw_runtime}
        )

        logger.info(
            "build app container: base_root=%s runtimes=%s",
            base_root or "~/witty-service/",
            sorted(resolved_runtime_factories),
        )

        # ----------------------------------------------------------------
        # 步骤 4: 构建 Agent 服务
        # 提供统一的 Agent 操作接口 (start/stop/status)
        # ----------------------------------------------------------------
        resolved_agent_service = agent_service or cls._build_agent_service(
            runtime_instance_manager=resolved_runtime_instance_manager,
            runtime_factories=resolved_runtime_factories,
        )

        # ----------------------------------------------------------------
        # 步骤 5: 构建 Session 服务
        # 提供统一的 Session 生命周期管理
        # ----------------------------------------------------------------
        resolved_session_service = session_service or cls._build_session_service(
            runtime_factories=resolved_runtime_factories,
            runtime_instance_manager=resolved_runtime_instance_manager,
        )

        # ----------------------------------------------------------------
        # 步骤 6: 构建状态同步服务
        # 负责 WebSocket 连接的状态推送
        # ----------------------------------------------------------------
        resolved_state_sync_service: SessionStateEventPublisherBase = (
            state_sync_service or SessionStateSyncService()
        )

        # ----------------------------------------------------------------
        # 步骤 7: 构建 WebSocket 编排器
        # 协调 Session 消息处理流程
        # ----------------------------------------------------------------
        session_ws_orchestrator: SessionTurnExecutorBase = SessionWSOrchestrator(
            session_service=resolved_session_service,
            agent_service=resolved_agent_service,
            state_sync_service=resolved_state_sync_service,
        )

        # ----------------------------------------------------------------
        # 步骤 8: 构建任务池
        # 管理并发任务执行，确保同 session 串行，不同 session 并发
        # ----------------------------------------------------------------
        task_pool: SessionTaskPoolBase = TaskPool(orchestrator=session_ws_orchestrator)

        return cls(
            runtime_instance_manager=resolved_runtime_instance_manager,
            runtime_registry=resolved_session_service.runtime_registry,
            runtime_factories=resolved_runtime_factories,
            agent_service=resolved_agent_service,
            session_service=resolved_session_service,
            state_sync_service=resolved_state_sync_service,
            session_ws_orchestrator=session_ws_orchestrator,
            task_pool=task_pool,
        )

    def create_agent_router(self) -> APIRouter:
        """创建 agent router。

        创建用于 Agent 管理的 FastAPI 路由器，提供以下端点:
        - POST /agent/start    : 启动 agent
        - POST /agent/stop     : 停止 agent
        - GET  /agent/status   : 获取 agent 状态
        - GET  /agent/skills   : 获取 agent 技能列表
        - GET  /agent/list     : 列出所有 agents

        Returns:
            配置好的 Agent APIRouter 实例
        """
        return create_agent_router(self.agent_service)

    def create_session_router(self) -> APIRouter:
        """创建 session router。

        创建用于 Session 管理的 FastAPI 路由器，提供以下端点:
        - POST   /agents/{agent_id}/sessions                      : 创建会话
        - GET    /agents/{agent_id}/sessions                      : 列出会话
        - GET    /agents/{agent_id}/sessions/{session_id}         : 获取会话详情
        - POST   /agents/{agent_id}/sessions/{session_id}/delete  : 删除会话
        - POST   /agents/{agent_id}/sessions/{session_id}/abort   : 中止会话
        - GET    /agents/{agent_id}/sessions/{session_id}/events  : 获取会话事件

        Returns:
            配置好的 Session APIRouter 实例
        """
        return create_session_router(
            self.session_service,
            state_sync_service=self.state_sync_service,
            agent_service=self.agent_service,
        )

    def create_session_ws_router(self) -> APIRouter:
        """创建 session websocket router。

        创建用于实时消息交互的 WebSocket 路由器:
        - WS /agents/{agent_id}/sessions/{session_id}/ws : WebSocket 实时消息通道

        WebSocket 消息格式:
        - 客户端发送: {"type": "message.create", "payload": {"message": "..."}}
        - 服务端推送: {"type": "message.delta", "payload": {"delta": "..."}}

        Returns:
            配置好的 Session WebSocket APIRouter 实例
        """
        return create_session_ws_router(
            task_pool=self.task_pool,
            state_sync_service=self.state_sync_service,
        )

    @staticmethod
    def _build_session_service(
        *,
        runtime_factories: Mapping[RuntimeType, RuntimeFactory],
        runtime_instance_manager: RuntimeInstanceManager,
    ) -> SessionService:
        """构建 session service，并按运行时工厂注册 runtime。

        该方法负责:
        1. 创建运行时注册表 (RuntimeRegistry)
        2. 创建会话存储仓库 (InMemorySessionRepository)
        3. 为每个 runtime 类型创建对应的 Session 服务
        4. 注册 runtime 实例到对应的服务
        5. 构建统一的 Session 服务门面

        Session 服务架构:
        ┌─────────────────────────────────────────────────────────┐
        │ SessionService (Facade)                                 │
        │   ├── binding_resolver: AgentBindingResolver            │
        │   └── services: Mapping[RuntimeType, SessionServiceBase]│
        │         ├── "openclaw" → OpenClawSessionService         │
        │         └── "opencode" → OpenCodeSessionService         │
        └─────────────────────────────────────────────────────────┘

        Args:
            binding_resolver: 绑定解析器，用于路由 session 请求
            runtime_factories: 运行时工厂映射

        Returns:
            完全初始化的 Session 服务实例
        """
        from witty_agent_server.application.services.session_to_runtime import (
            runtime_registry,
        )

        # 创建运行时注册表
        runtime_registry_instance = runtime_registry.RuntimeRegistry()

        # 创建会话存储仓库（内存实现）
        repository = InMemorySessionRepository()

        # 为每个 runtime 类型创建对应的 Session 服务
        services: dict[RuntimeType, SessionServiceBase] = {}
        for runtime_type, runtime_factory in runtime_factories.items():
            logger.info("register session runtime: runtime=%s", runtime_type)

            # 创建 runtime 实例
            if runtime_type == "openclaw":
                runtime = runtime_factory(
                    runtime_instance_manager=runtime_instance_manager
                )
            else:
                runtime = runtime_factory()

            # 注册到全局注册表
            runtime_registry_instance.register(runtime)

            # 根据 runtime 类型创建对应的服务
            service: SessionServiceBase
            if runtime_type == "opencode":
                service = OpenCodeSessionService(
                    runtime_registry=runtime_registry_instance,
                    repository=repository,
                )
            else:
                # 默认使用 OpenClawSessionService
                service = OpenClawSessionService(
                    runtime_registry=runtime_registry_instance,
                    repository=repository,
                )

            # 将 runtime 注册到服务
            service.register_runtime(runtime)
            services[runtime_type] = service

        # 构建统一的 Session 服务门面
        return SessionService(
            services=services,
            runtime_instance_manager=runtime_instance_manager,
        )

    @staticmethod
    def _build_agent_service(
        *,
        runtime_instance_manager: RuntimeInstanceManager,
        runtime_factories: Mapping[RuntimeType, RuntimeFactory],
    ) -> AgentService:
        """构建 agent facade，并按 runtime 注册底层 service。

        该方法负责:
        1. 为每个 runtime 类型创建对应的 Agent 服务
        2. 注入必要的依赖（绑定解析器、实例管理器等）
        3. 构建统一的 Agent 服务门面

        Agent 服务架构:
        ┌─────────────────────────────────────────────────────────┐
        │ AgentService (Facade)                                   │
        │   ├── binding_resolver: AgentBindingResolver            │
        │   └── services: Mapping[RuntimeType, AgentServiceBase]  │
        │         ├── "openclaw" → OpenClawAgentService           │
        │         │                  ├── binding_resolver         │
        │         │                  └── runtime_instance_manager │
        │         └── "opencode" → OpenCodeAgentService           │
        └─────────────────────────────────────────────────────────┘

        注意:
        - OpenClawAgentService 需要 binding 和实例管理依赖
        - OpenCodeAgentService 是轻量级实现，不需要额外依赖

        Args:
            binding_resolver: 绑定解析器，用于路由 agent 请求
            runtime_instance_manager: 运行时实例管理器
            runtime_factories: 运行时工厂映射
        Returns:
            完全初始化的 Agent 服务实例
        """
        services: dict[RuntimeType, OpenClawAgentService | OpenCodeAgentService] = {}

        # 为每个 runtime 类型创建对应的 Agent 服务
        for runtime_type in runtime_factories:
            logger.info("register agent service: runtime=%s", runtime_type)

            if runtime_type == "opencode":
                # OpenCode 是轻量级实现，不需要额外依赖
                services[runtime_type] = OpenCodeAgentService()
                continue

            # OpenClaw 需要完整的工作空间和上下文管理依赖
            services[runtime_type] = OpenClawAgentService(
                runtime_instance_manager=runtime_instance_manager,
            )

        # 构建统一的 Agent 服务门面
        return AgentService(
            services=services,
            runtime_instance_manager=runtime_instance_manager,
        )

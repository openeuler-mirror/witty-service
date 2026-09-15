"""W20 假链路端到端：假渠道 -> 真路由/真网关/真仓储 -> 假 agent -> 假渠道。

覆盖一条消息走完 **入站 -> 去重 -> 准入 -> 路由 -> 提交 -> 终态 -> 出站** 全链路，
并断言出站内容与顺序；同时覆盖重复投递、命令就地处理、准入拒绝与长回合延迟补发。

与单元测试的区别：这里用的是**真实的** `SessionRouter` + `ChannelGateway` +
`AgentTurnGateway` + `ChannelRepository` + SQLite，只有"渠道"与"agent"是替身，
因此它能证明这些组件装配在一起时的行为（含会话绑定、来源标记、投递归档）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import witty_service.config as _config
from tests.unit.channels.fakes import (
    FakeAdapter,
    FakeAgentManager,
    FakeTurnRepository,
    FakeTurnScript,
)
from witty_service.api.services import ServiceContainer
from witty_service.channels import commands as cmd
from witty_service.channels.contracts import (
    CONVERSATION_TYPE_DIRECT,
    InboundMessage,
    Route,
)
from witty_service.channels.gateway import ChannelGateway
from witty_service.channels.router import SessionRouter
from witty_service.channels.turn_gateway import AgentTurnGateway
from witty_service.config import get_settings
from witty_service.domain.enums import AgentStatus
from witty_service.persistence.channel_repository import ChannelRepository
from witty_service.persistence.db import create_session_factory, create_sqlite_engine
from witty_service.persistence.orm import Base
from witty_service.persistence.repositories import SqliteRepository

AGENT_ID = "agent-1"
CREDENTIALS = {"bot_id": "e2e-bot-1234567890", "secret": "e2e-secret-value"}


class BridgedAgentManager(FakeAgentManager):
    """假 agent：会话与运行时标识写进**真实仓储**，回合由脚本驱动（不依赖真实 LLM）。

    `AgentTurnGateway` 的提交前自愈会检查"会话行存在 + `remote_runtime_agent_id`
    非空"，因此假 agent 必须把会话落到真实仓储里，否则每一次提交都会重建会话。
    """

    def __init__(self, repository: SqliteRepository) -> None:
        super().__init__(FakeTurnRepository())
        self._real = repository

    async def create_session(  # type: ignore[override]
        self, agent_id: str, runtime_agent_id: str | None = None
    ) -> object:
        session = self._real.create_session(agent_id)
        self._real.upsert_session(
            session.id,
            agent_id,
            status="idle",
            remote_runtime_agent_id=runtime_agent_id or "runtime-agent-e2e",
        )
        self.created_sessions.append(session.id)
        return session

    async def resume_agent(self, agent_id: str) -> object:  # type: ignore[override]
        self.resumed.append(agent_id)
        return self._real.get_agent(agent_id)


@dataclass
class Env:
    services: ServiceContainer
    gateway: ChannelGateway
    router: SessionRouter
    repository: ChannelRepository
    sessions: SqliteRepository
    manager: BridgedAgentManager
    adapter: FakeAdapter = field(default_factory=FakeAdapter)
    instance_id: str = ""
    counter: int = 0

    def route(self, user: str = "u1") -> Route:
        return Route(
            instance_id=self.instance_id,
            conversation_type=CONVERSATION_TYPE_DIRECT,
            platform_user_id=user,
        )

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0)

    async def emit(self, text: str | None, *, user: str = "u1", event_id: str | None = None) -> None:
        self.counter += 1
        await self.adapter.emit(
            InboundMessage(
                platform_event_id=event_id or f"evt-{self.counter}",
                route=self.route(user),
                text=text,
                received_at=datetime.now(UTC),
            )
        )

    async def idle(self, user: str = "u1") -> bool:
        return await self.router.wait_until_idle(self.route(user), timeout=3.0)


def _build(tmp_path: Path, monkeypatch, *, stall_window: float = 30.0) -> Env:
    monkeypatch.setenv(
        "WITTY_CHANNEL_CREDENTIALS_DIR", str(tmp_path / "channel-credentials")
    )
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setattr(_config, "_settings", None)

    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'e2e.sqlite3'}")
    Base.metadata.create_all(engine)
    sessions = SqliteRepository(create_session_factory(engine))
    sessions.create_agent_with_id(
        agent_id=AGENT_ID,
        name="演示 agent",
        sandbox_type="local_process",
        adapter_type="openclaw",
        workspace_path=str(tmp_path / "workspace"),
        idle_timeout_seconds=300,
        status=AgentStatus.running,
    )

    services = ServiceContainer(repository=sessions, workspace_store=MagicMock())
    manager = BridgedAgentManager(sessions)
    adapter = FakeAdapter()

    def builder(adapter_cls: object, **kwargs: object) -> FakeAdapter:
        del adapter_cls, kwargs
        return adapter

    async def never_sleep(_delay: float) -> None:
        await asyncio.Event().wait()

    # 与生产装配同构：真实路由 + 真实回合网关 + 真实仓储，只有渠道与 agent 是替身
    router = SessionRouter(
        repository=services.channel_repository,
        gateway=AgentTurnGateway(
            repository=sessions,
            get_agent_manager=lambda _agent_id: manager,
            channel="",
        ),
        dedup=services.channel_dedup,
        queue_depth=3,
        stall_window_seconds=stall_window,
    )
    gateway = ChannelGateway(
        repository=services.channel_repository,
        router=router,
        settings=get_settings().channel,
        dedup=services.channel_dedup,
        adapter_builder=builder,
        worker_count=lambda: 1,
        sleep=never_sleep,
        cleanup_interval_seconds=10_000.0,
    )
    services.channel_router = router
    services.channel_gateway = gateway
    return Env(
        services=services,
        gateway=gateway,
        router=router,
        repository=services.channel_repository,
        sessions=sessions,
        manager=manager,
        adapter=adapter,
    )


async def _ready(tmp_path: Path, monkeypatch, *, stall_window: float = 30.0) -> Env:
    env = _build(tmp_path, monkeypatch, stall_window=stall_window)
    assert await env.gateway.start() is True
    record = await env.services.get_channel_manual_binder().bind(
        channel="wecom_bot", credentials=CREDENTIALS, agent_id=AGENT_ID
    )
    env.instance_id = record.id
    await env.gateway.reload()
    await env.settle()
    return env


# ==============================================================================
# 全链路
# ==============================================================================


@pytest.mark.asyncio
async def test_message_walks_the_whole_pipeline(tmp_path, monkeypatch) -> None:
    env = await _ready(tmp_path, monkeypatch)

    await env.emit("帮我看看这个报错")
    assert await env.idle()
    await env.gateway.stop()

    # 提交：一次，且内容与路由绑定
    assert env.manager.submitted == [
        (AGENT_ID, env.manager.created_sessions[0], "帮我看看这个报错")
    ]
    # 出站顺序：占位消息 -> 终稿
    assert env.adapter.texts == [cmd.PLACEHOLDER_TEXT, "最终结果"]
    # 会话绑定与来源标记都落了库
    route = env.repository.list_routes_for_instance(env.instance_id)[0]
    assert route.active_session_id == env.manager.created_sessions[0]
    assert route.active_placeholder_ref is None  # 终稿投递完成后清空
    session = env.sessions.get_session(route.active_session_id or "")
    assert session is not None
    assert session.origin == "channel:wecom_bot"
    # 投递按三态归档
    deliveries = env.repository.list_deliveries(instance_id=env.instance_id)
    assert [item.certainty for item in deliveries] == ["delivered", "delivered"]
    assert all(item.turn_id for item in deliveries)


@pytest.mark.asyncio
async def test_duplicate_delivery_is_processed_once(tmp_path, monkeypatch) -> None:
    env = await _ready(tmp_path, monkeypatch)

    await env.emit("你好", event_id="evt-dup")
    assert await env.idle()
    await env.emit("你好", event_id="evt-dup")  # 平台重推
    assert await env.idle()
    await env.gateway.stop()

    assert len(env.manager.submitted) == 1
    assert env.adapter.texts == [cmd.PLACEHOLDER_TEXT, "最终结果"]


@pytest.mark.asyncio
async def test_commands_are_handled_in_place(tmp_path, monkeypatch) -> None:
    env = await _ready(tmp_path, monkeypatch)

    await env.emit("你好")
    assert await env.idle()
    env.adapter.calls.clear()

    await env.emit("/status")
    # 命令不入队：同步处理完，队列深度始终为 0
    assert env.router.queue_depth(env.route()) == 0
    await env.emit("/new")
    assert env.router.queue_depth(env.route()) == 0
    await env.gateway.stop()

    assert "当前状态" in env.adapter.texts[0]
    assert "演示 agent" in env.adapter.texts[0]
    assert env.adapter.texts[1] == cmd.NEW_ACK_TEXT
    # /new 之后的路由绑定指向新会话
    route = env.repository.list_routes_for_instance(env.instance_id)[0]
    assert route.active_session_id == env.manager.created_sessions[-1]
    assert len(env.manager.created_sessions) == 2


@pytest.mark.asyncio
async def test_access_policy_denial_is_replied_and_not_queued(tmp_path, monkeypatch) -> None:
    env = await _ready(tmp_path, monkeypatch)
    env.repository.upsert_access_policy(
        instance_id=env.instance_id,
        conversation_type=CONVERSATION_TYPE_DIRECT,
        mode="allowlist",
        allowlist=["someone-else"],
    )

    await env.emit("你好")
    assert await env.idle()
    await env.gateway.stop()

    assert env.adapter.texts == [cmd.ACCESS_DENIED_TEXT]
    assert env.manager.submitted == []
    assert env.router.queue_depth(env.route()) == 0


@pytest.mark.asyncio
async def test_long_turn_defers_then_delivers(tmp_path, monkeypatch) -> None:
    """长回合：停滞窗口到期后先告知用户，终态到达再补发终稿（两条都要按顺序）。"""
    env = await _ready(tmp_path, monkeypatch, stall_window=0.05)
    env.manager.default_script = FakeTurnScript(final_text="慢结果", terminal_delay=0.5)

    await env.emit("写一份长报告")
    assert await env.idle()
    await env.gateway.stop()

    assert env.adapter.texts[0] == cmd.PLACEHOLDER_TEXT
    assert cmd.DEFERRED_NOTICE_TEXT in env.adapter.texts
    assert env.adapter.texts[-1] == "慢结果"


@pytest.mark.asyncio
async def test_unsupported_content_gets_degraded_reply(tmp_path, monkeypatch) -> None:
    env = await _ready(tmp_path, monkeypatch)

    await env.adapter.emit(
        InboundMessage(
            platform_event_id="evt-image",
            route=env.route(),
            text=None,
            unsupported_kind="image",
            received_at=datetime.now(UTC),
        )
    )
    assert await env.idle()
    await env.gateway.stop()

    assert "图片" in env.adapter.texts[0]
    assert env.manager.submitted == []

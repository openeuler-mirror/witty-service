"""QQ 的被动回复额度是**总出站条数**（占位消息 + 终稿分段 ≤ 4，单聊）。

回归用例把平台帧喂进真实路由器，断言**一个回合的总出站条数不超过平台额度，且每一条
都是被动回复**。单元测试各自只覆盖一半（`delivery.plan` 只管分段、`QqBotAdapter` 只管
被动／主动），相加是否越界只有端到端才看得出来——4 段终稿加占位消息就是 5 条，第 5 条
会被降级成需要用户先授权的主动消息，大概率被拒，用户拿到半截答案。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.unit.channels.fakes import FakeTurnGateway, FakeTurnScript
from witty_service.channels import commands as cmd
from witty_service.channels.adapters.qq_bot import (
    EVENT_C2C_MESSAGE_CREATE,
    MAX_REPLY_SEGMENTS,
    PASSIVE_REPLY_RULES,
    QqBotAdapter,
)
from witty_service.channels.contracts import (
    CONVERSATION_TYPE_DIRECT,
    Route,
)
from witty_service.channels.dedup import InboundDedup
from witty_service.channels.router import SessionRouter
from witty_service.persistence.channel_repository import ChannelRepository
from witty_service.persistence.db import create_session_factory, create_sqlite_engine
from witty_service.persistence.orm import Base

INSTANCE_ID = "instance-qq"
AGENT_ID = "agent-1"
USER_OPENID = "OPENID_1"
#: 单聊额度：官方文档《消息收发概述 · 频率与时效规则》4 条 / 60 分钟
DIRECT_LIMIT = PASSIVE_REPLY_RULES[CONVERSATION_TYPE_DIRECT][1]

#: 6 个段落 ≈ 14k 字符：分段后必然超过"限额 - 预留"，从而触发提示条
LONG_ANSWER = ("段落。" * 800 + "\n\n") * 6


class RecordingTransport:
    """假传输层：记录每一次出站，并允许测试把平台帧推进来。"""

    def __init__(self) -> None:
        self.connected = False
        self.closed = False
        self.sent: list[dict[str, Any]] = []
        self._handler = None

    def on_event(self, handler) -> None:
        self._handler = handler

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    def is_alive(self) -> bool:
        return self.connected and not self.closed

    async def send_message(self, **kwargs: object) -> dict[str, Any]:
        self.sent.append(dict(kwargs))
        return {"id": f"MSG_OUT_{len(self.sent)}"}

    def push(self, frame: dict[str, Any]) -> None:
        assert self._handler is not None, "transport was never connected"
        self._handler(frame)

    def texts(self) -> list[str]:
        return [str(item["text"]) for item in self.sent]

    def passive(self) -> list[dict[str, Any]]:
        return [item for item in self.sent if item["msg_id"] is not None]

    def proactive(self) -> list[dict[str, Any]]:
        return [item for item in self.sent if item["msg_id"] is None]


@dataclass
class Env:
    router: SessionRouter
    adapter: QqBotAdapter
    transport: RecordingTransport
    route: Route

    async def deliver(self, frame: dict[str, Any], *, timeout: float = 5.0) -> bool:
        """把一条平台帧交给适配器，然后等到这个回合真的跑完。

        顺序不能省：适配器把入站交给管线是**异步**的（`emit_inbound` 起一个任务），
        不等它落地就 `wait_until_idle`，会因为"这条路由还没有执行体"而立刻返回真。
        """
        self.transport.push(frame)
        await self.adapter.wait_for_inbound()
        return await self.router.wait_until_idle(self.route, timeout=timeout)


def _frame(event_id: str = "EVENT_1", msg_id: str = "MSG_1") -> dict[str, Any]:
    return {
        "id": event_id,
        "op": 0,
        "t": EVENT_C2C_MESSAGE_CREATE,
        "d": {
            "id": msg_id,
            "content": "给我一份长报告",
            "timestamp": datetime.now(UTC).isoformat(),
            "author": {"user_openid": USER_OPENID},
        },
    }


def _build_env(tmp_path, *, stall_window: float, script: FakeTurnScript) -> Env:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'qq_budget.sqlite3'}")
    Base.metadata.create_all(engine)
    repository = ChannelRepository(create_session_factory(engine))
    instance = repository.create_instance(channel="qq_bot", agent_id=AGENT_ID)
    transport = RecordingTransport()
    adapter = QqBotAdapter(
        instance_id=instance.id,
        config={"app_id": "102123456"},
        credentials={"secret": "s3cr3t"},
        transport_factory=lambda _app_id, _secret: transport,
    )
    gateway = FakeTurnGateway(
        agent_states={AGENT_ID: "running"}, agent_names={AGENT_ID: "demo-agent"}
    )
    gateway.push_script(script)
    router = SessionRouter(
        repository=repository,
        gateway=gateway,
        dedup=InboundDedup(repository),
        stall_window_seconds=stall_window,
    )
    router.register_instance(
        instance.id,
        channel="qq_bot",
        adapter=adapter,
        generation=instance.generation,
        agent_id=AGENT_ID,
    )
    adapter.on_inbound(router.handle_inbound)
    return Env(
        router=router,
        adapter=adapter,
        transport=transport,
        route=Route(
            instance_id=instance.id,
            conversation_type=CONVERSATION_TYPE_DIRECT,
            platform_user_id=USER_OPENID,
        ),
    )


@pytest.mark.asyncio
async def test_a_long_answer_never_exceeds_the_platform_quota(tmp_path) -> None:
    """占位消息也算一条：4 段终稿必须收成 3 条，而不是"4 段 + 1 条主动消息"。"""
    env = _build_env(
        tmp_path,
        stall_window=30.0,
        script=FakeTurnScript(final_text=LONG_ANSWER),
    )
    await env.adapter.start()

    assert await env.deliver(_frame()) is True

    texts = env.transport.texts()
    assert texts[0] == cmd.PLACEHOLDER_TEXT
    assert len(texts) == DIRECT_LIMIT == MAX_REPLY_SEGMENTS
    # 全部是被动回复：任何一条主动消息都意味着额度已经被撞穿
    assert env.transport.proactive() == []
    assert [item["msg_seq"] for item in env.transport.passive()] == [1, 2, 3, 4]
    assert texts[-1] == cmd.CONSOLE_NOTICE_TEXT
    await env.adapter.stop()


@pytest.mark.asyncio
async def test_a_stalled_turn_sends_only_one_notice_and_still_fits(tmp_path) -> None:
    """停滞提示也占额度，因此每回合只发一条；终稿再据此少分一段。"""
    env = _build_env(
        tmp_path,
        stall_window=0.05,
        script=FakeTurnScript(final_text=LONG_ANSWER, terminal_delay=0.3),
    )
    await env.adapter.start()

    assert await env.deliver(_frame()) is True

    texts = env.transport.texts()
    assert texts.count(cmd.DEFERRED_NOTICE_TEXT) == 1
    assert len(texts) == DIRECT_LIMIT
    assert env.transport.proactive() == []
    assert [item["msg_seq"] for item in env.transport.passive()] == [1, 2, 3, 4]
    await env.adapter.stop()

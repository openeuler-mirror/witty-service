"""W10 会话路由的测试：覆盖 9.1 的第 6、7、10、12、13 条，以及入站管线（§4.5）。

全部用 `fakes.FakeAdapter` + `fakes.FakeTurnGateway` 驱动，不依赖真实 LLM 与平台。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from tests.unit.channels.fakes import (
    FakeAdapter,
    FakeTurnGateway,
    FakeTurnScript,
    delta_event,
    error_event,
    question_event,
)
from witty_service.channels import commands as cmd
from witty_service.channels import errors as err
from witty_service.channels.contracts import (
    CONVERSATION_TYPE_DIRECT,
    CONVERSATION_TYPE_GROUP,
    ChannelCapabilities,
    DeliveryResult,
    InboundMessage,
    Route,
)
from witty_service.channels.dedup import InboundDedup
from witty_service.channels.router import SessionRouter
from witty_service.domain.errors import DomainError
from witty_service.persistence.channel_repository import ChannelRepository
from witty_service.persistence.db import create_session_factory, create_sqlite_engine
from witty_service.persistence.orm import Base

STALL_WINDOW = 0.05


@dataclass
class Env:
    router: SessionRouter
    repository: ChannelRepository
    adapter: FakeAdapter
    gateway: FakeTurnGateway
    instance_id: str
    agent_id: str
    counter: int = 0

    def route(self, user: str = "u1", conversation_type: str = CONVERSATION_TYPE_DIRECT) -> Route:
        return Route(
            instance_id=self.instance_id,
            conversation_type=conversation_type,
            platform_user_id=user,
        )

    def next_event_id(self) -> str:
        self.counter += 1
        return f"evt-{self.counter}"

    async def send(
        self,
        text: str | None,
        *,
        user: str = "u1",
        event_id: str | None = None,
        conversation_type: str = CONVERSATION_TYPE_DIRECT,
        unsupported_kind: str | None = None,
    ) -> None:
        await self.router.handle_inbound(
            InboundMessage(
                platform_event_id=event_id or self.next_event_id(),
                route=self.route(user, conversation_type),
                text=text,
                unsupported_kind=unsupported_kind,
                received_at=datetime.now(UTC),
            )
        )

    async def idle(self, user: str = "u1", *, timeout: float = 5.0) -> bool:
        return await self.router.wait_until_idle(self.route(user), timeout=timeout)


def _build_env(
    tmp_path,
    *,
    capabilities: ChannelCapabilities | None = None,
    queue_depth: int = 3,
    stall_window: float = STALL_WINDOW,
    agent_id: str | None = "agent-1",
    send_results: list[DeliveryResult] | None = None,
    register_instance: bool = True,
) -> Env:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'router.sqlite3'}")
    Base.metadata.create_all(engine)
    repository = ChannelRepository(create_session_factory(engine))
    instance = repository.create_instance(channel="fake_bot", agent_id=agent_id)
    adapter = FakeAdapter(capabilities=capabilities, send_results=send_results)
    gateway = FakeTurnGateway(
        agent_states={"agent-1": "running"}, agent_names={"agent-1": "demo-agent"}
    )
    router = SessionRouter(
        repository=repository,
        gateway=gateway,
        dedup=InboundDedup(repository),
        queue_depth=queue_depth,
        stall_window_seconds=stall_window,
    )
    if register_instance:
        router.register_instance(
            instance.id,
            channel="fake_bot",
            adapter=adapter,
            generation=instance.generation,
            agent_id=agent_id,
        )
    return Env(
        router=router,
        repository=repository,
        adapter=adapter,
        gateway=gateway,
        instance_id=instance.id,
        agent_id="agent-1",
    )


# ==============================================================================
# 连通性测试（非回合出站）
# ==============================================================================


@pytest.mark.asyncio
async def test_control_text_reports_offline_instead_of_not_found(tmp_path) -> None:
    """实例存在但没连上：报"离线"，而不是"实例不存在"——处置完全不同。"""
    env = _build_env(tmp_path, register_instance=False)
    with pytest.raises(DomainError) as excinfo:
        await env.router.send_control_text(
            instance_id=env.instance_id, route=env.route(), text="ping"
        )
    assert excinfo.value.code == err.CHANNEL_INSTANCE_OFFLINE
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_control_text_reports_not_found_for_unknown_instance(tmp_path) -> None:
    env = _build_env(tmp_path)
    with pytest.raises(DomainError) as excinfo:
        await env.router.send_control_text(
            instance_id="missing", route=env.route(), text="ping"
        )
    assert excinfo.value.code == err.CHANNEL_INSTANCE_NOT_FOUND


@pytest.mark.asyncio
async def test_control_text_does_not_submit_a_turn(tmp_path) -> None:
    env = _build_env(tmp_path)
    result = await env.router.send_control_text(
        instance_id=env.instance_id, route=env.route(), text=cmd.CONNECTIVITY_TEST_TEXT
    )
    assert result.delivered
    assert env.adapter.texts == [cmd.CONNECTIVITY_TEST_TEXT]
    assert env.gateway.turns == []


# ==============================================================================
# 基本链路
# ==============================================================================


@pytest.mark.asyncio
async def test_direct_message_runs_turn_and_delivers_final(tmp_path) -> None:
    env = _build_env(tmp_path)

    await env.send("帮我看看")
    assert await env.idle()

    assert env.gateway.turns == [("agent-1", "session-1", "帮我看看")]
    # 不能原地编辑的渠道：占位消息 + 补发终稿
    assert env.adapter.texts == [cmd.PLACEHOLDER_TEXT, "最终结果"]
    assert env.adapter.edits == []


@pytest.mark.asyncio
async def test_duplicate_event_processed_once(tmp_path) -> None:
    """同一条平台消息投递两次，只处理一次（9.1 第 8 条）。"""
    env = _build_env(tmp_path)

    await env.send("你好", event_id="evt-same")
    await env.send("你好", event_id="evt-same")
    assert await env.idle()

    assert len(env.gateway.turns) == 1
    assert env.adapter.texts.count(cmd.PLACEHOLDER_TEXT) == 1


@pytest.mark.asyncio
async def test_group_message_is_dropped_without_reply(tmp_path) -> None:
    """群聊是"不回复"的唯一例外（特性设计文档 7.3）。"""
    env = _build_env(tmp_path)

    await env.send("大家好", conversation_type=CONVERSATION_TYPE_GROUP)
    assert await env.idle()

    assert env.adapter.calls == []
    assert env.gateway.turns == []
    # 去重登记在群聊判定之前：便宜且先做，避免重复刷日志
    assert env.repository.count_inbound_events(instance_id=env.instance_id) == 1


@pytest.mark.asyncio
async def test_unsupported_content_gets_explicit_reply(tmp_path) -> None:
    env = _build_env(tmp_path)

    await env.send(None, unsupported_kind="image")
    assert await env.idle()

    assert env.adapter.texts == [cmd.render_unsupported_content("image")]
    assert env.gateway.turns == []


@pytest.mark.asyncio
async def test_unknown_instance_is_dropped(tmp_path) -> None:
    env = _build_env(tmp_path, register_instance=False)

    await env.send("你好")
    assert await env.idle()

    assert env.adapter.calls == []


@pytest.mark.asyncio
async def test_stale_generation_is_dropped(tmp_path) -> None:
    """实例被删除后重建时，旧世代的长连接回调必须丢弃。"""
    env = _build_env(tmp_path)
    env.repository.update_instance(env.instance_id, bump_generation=True)

    await env.send("你好")
    assert await env.idle()

    assert env.adapter.calls == []
    assert env.gateway.turns == []


# ==============================================================================
# 准入
# ==============================================================================


@pytest.mark.asyncio
async def test_open_policy_allows_by_default(tmp_path) -> None:
    env = _build_env(tmp_path)

    await env.send("你好")
    assert await env.idle()

    assert len(env.gateway.turns) == 1


@pytest.mark.asyncio
async def test_allowlist_policy_rejects_unknown_user(tmp_path) -> None:
    env = _build_env(tmp_path)
    env.repository.upsert_access_policy(
        instance_id=env.instance_id,
        conversation_type=CONVERSATION_TYPE_DIRECT,
        mode="allowlist",
        allowlist=["someone-else"],
    )

    await env.send("你好")
    assert await env.idle()

    assert env.gateway.turns == []
    assert env.adapter.texts == [cmd.ACCESS_DENIED_TEXT]


@pytest.mark.asyncio
async def test_access_policy_write_takes_effect_immediately(tmp_path) -> None:
    """准入是业务数据：写入后立即生效，无需重启（不做进程内快照）。"""
    env = _build_env(tmp_path)
    await env.send("第一条")
    assert await env.idle()
    assert len(env.gateway.turns) == 1

    env.repository.upsert_access_policy(
        instance_id=env.instance_id,
        conversation_type=CONVERSATION_TYPE_DIRECT,
        mode="allowlist",
        allowlist=[],
    )
    await env.send("第二条")
    assert await env.idle()

    assert len(env.gateway.turns) == 1
    assert env.adapter.texts[-1] == cmd.ACCESS_DENIED_TEXT


@pytest.mark.asyncio
async def test_corrupt_policy_fails_closed(tmp_path) -> None:
    env = _build_env(tmp_path)
    env.repository.upsert_access_policy(
        instance_id=env.instance_id,
        conversation_type=CONVERSATION_TYPE_DIRECT,
        mode="allowlist",
        allowlist=[],
    )

    await env.send("你好")
    assert await env.idle()

    assert env.gateway.turns == []


# ==============================================================================
# 会话路由（9.1 第 6、7 条）
# ==============================================================================


@pytest.mark.asyncio
async def test_same_route_reuses_session(tmp_path) -> None:
    env = _build_env(tmp_path)

    await env.send("第一条")
    assert await env.idle()
    await env.send("第二条")
    assert await env.idle()

    assert [turn[1] for turn in env.gateway.turns] == ["session-1", "session-1"]


@pytest.mark.asyncio
async def test_distinct_routes_isolated(tmp_path) -> None:
    env = _build_env(tmp_path)

    await env.send("甲的提问", user="u1")
    assert await env.idle("u1")
    await env.send("乙的提问", user="u2")
    assert await env.idle("u2")

    sessions = {turn[2]: turn[1] for turn in env.gateway.turns}
    assert sessions["甲的提问"] != sessions["乙的提问"]


@pytest.mark.asyncio
async def test_new_command_rotates_active_session(tmp_path) -> None:
    """开启新会话后，新消息进入新会话，旧会话不被写入（9.1 第 7 条）。"""
    env = _build_env(tmp_path)
    await env.send("旧会话消息")
    assert await env.idle()
    old_session = env.gateway.turns[0][1]

    await env.send("/new")
    assert await env.idle()
    await env.send("新会话消息")
    assert await env.idle()

    new_session = env.gateway.turns[-1][1]
    assert new_session != old_session
    assert env.gateway.turns[-1][2] == "新会话消息"
    record = env.repository.list_routes_for_instance(env.instance_id)[0]
    assert record.active_session_id == new_session
    assert any(cmd.NEW_ACK_TEXT in text for text in env.adapter.texts)


@pytest.mark.asyncio
async def test_session_rebuild_when_binding_is_stale(tmp_path) -> None:
    """绑定的会话行已不存在时，提交前自愈重建（§4.4）。"""
    env = _build_env(tmp_path)
    binding = env.repository.get_or_create_route(
        instance_id=env.instance_id,
        conversation_type=CONVERSATION_TYPE_DIRECT,
        platform_user_id="u1",
    )
    env.repository.set_active_session(binding.id, "ghost")

    await env.send("你好")
    assert await env.idle()

    assert env.gateway.resolved[0] == ("agent-1", "ghost")
    assert env.gateway.turns[0][1] != "ghost"
    # 新会话立即写回路由绑定
    refreshed = env.repository.get_route(binding.id)
    assert refreshed is not None
    assert refreshed.active_session_id == env.gateway.turns[0][1]


# ==============================================================================
# 队列（9.1 第 13 条）
# ==============================================================================


@pytest.mark.asyncio
async def test_queue_full_rejects_without_persisting(tmp_path) -> None:
    env = _build_env(tmp_path, queue_depth=1)
    env.gateway.push_script(FakeTurnScript(terminal_delay=0.2, final_text="慢结果"))

    await env.send("第一条")
    await asyncio.sleep(0.02)
    await env.send("第二条")  # 排队数 = 1，仍在深度内
    await env.send("第三条")  # 超上限 -> 不受理

    assert cmd.render_queue_full(depth=1) in env.adapter.texts
    await env.idle()
    submitted = [turn[2] for turn in env.gateway.turns]
    assert submitted == ["第一条", "第二条"]


@pytest.mark.asyncio
async def test_no_placeholder_while_queued(tmp_path) -> None:
    """排队期间不发占位消息，轮到自己时才发（9.1 第 13 条）。"""
    env = _build_env(tmp_path)
    env.gateway.push_script(FakeTurnScript(terminal_delay=0.2, final_text="慢结果"))

    await env.send("第一条")
    await asyncio.sleep(0.02)
    await env.send("第二条")
    await asyncio.sleep(0.02)

    assert env.adapter.texts.count(cmd.PLACEHOLDER_TEXT) == 1

    await env.idle()
    assert env.adapter.texts.count(cmd.PLACEHOLDER_TEXT) == 2


@pytest.mark.asyncio
async def test_queue_depth_snapshot(tmp_path) -> None:
    env = _build_env(tmp_path)
    env.gateway.push_script(FakeTurnScript(terminal_delay=0.2, final_text="慢结果"))

    await env.send("第一条")
    await asyncio.sleep(0.02)
    assert env.router.queue_depth(env.route()) == 0
    await env.send("第二条")
    assert env.router.queue_depth(env.route()) == 1

    await env.idle()


@pytest.mark.asyncio
async def test_stop_drains_queue_and_notifies(tmp_path) -> None:
    """停止清空队列并逐条告知被取消的消息（9.1 第 13 条）。"""
    env = _build_env(tmp_path)
    env.gateway.push_script(FakeTurnScript(terminal_delay=0.3, final_text="慢结果"))

    await env.send("第一条")
    await asyncio.sleep(0.02)
    await env.send("第二条")
    await env.send("第三条")

    await env.send("/stop")

    assert env.adapter.texts.count(cmd.STOP_CANCELLED_TEXT) == 2
    assert cmd.STOP_ACK_TEXT in env.adapter.texts
    assert env.gateway.aborted == [("agent-1", "session-1")]

    await env.idle()
    assert [turn[2] for turn in env.gateway.turns] == ["第一条"]


@pytest.mark.asyncio
async def test_stop_without_active_turn_replies_idle(tmp_path) -> None:
    env = _build_env(tmp_path)

    await env.send("/stop")
    assert await env.idle()

    assert env.adapter.texts == [cmd.STOP_IDLE_ACK_TEXT]
    assert env.gateway.aborted == []


@pytest.mark.asyncio
async def test_commands_are_not_queued(tmp_path) -> None:
    """命令就地处理：队列被回合占用时也**立刻**得到答复（§4.3）。"""
    env = _build_env(tmp_path)
    env.gateway.push_script(FakeTurnScript(terminal_delay=0.2, final_text="慢结果"))

    await env.send("第一条")
    await asyncio.sleep(0.02)

    await env.send("/version")

    assert any("fake_bot" in text for text in env.adapter.texts)
    assert env.router.queue_depth(env.route()) == 0
    assert len(env.gateway.turns) == 1  # 命令不提交回合
    await env.idle()


# ==============================================================================
# 命令（9.1 第 11 条）
# ==============================================================================


@pytest.mark.asyncio
async def test_help_reports_missing_agent(tmp_path) -> None:
    env = _build_env(tmp_path, agent_id=None)

    await env.send("/help")
    assert await env.idle()

    assert cmd.AGENT_NOT_BOUND_TEXT in env.adapter.texts[0]


@pytest.mark.asyncio
async def test_version_reports_adapter_version(tmp_path) -> None:
    env = _build_env(tmp_path)

    await env.send("/version")
    assert await env.idle()

    assert "test-1.0" in env.adapter.texts[0]


@pytest.mark.asyncio
async def test_status_reports_queue_depth_and_session(tmp_path) -> None:
    env = _build_env(tmp_path)
    await env.send("第一条")
    assert await env.idle()

    env.gateway.push_script(FakeTurnScript(terminal_delay=0.2, final_text="慢结果"))
    await env.send("第二条")
    await asyncio.sleep(0.02)
    await env.send("第三条")

    await env.send("/status")

    status = env.adapter.texts[-1]
    assert "排队中的消息：1 条" in status
    assert "session-1"[:8] in status
    await env.idle()


@pytest.mark.asyncio
async def test_unknown_slash_command_goes_to_agent(tmp_path) -> None:
    """以 / 开头但不在表内 -> 按普通消息处理（特性设计文档第 5 节）。"""
    env = _build_env(tmp_path)

    await env.send("/deploy --now")
    assert await env.idle()

    assert env.gateway.turns == [("agent-1", "session-1", "/deploy --now")]


@pytest.mark.asyncio
async def test_agent_not_bound_gets_explicit_reply(tmp_path) -> None:
    env = _build_env(tmp_path, agent_id=None)

    await env.send("你好")
    assert await env.idle()

    assert env.adapter.texts == [cmd.AGENT_NOT_BOUND_TEXT]
    assert env.gateway.turns == []


# ==============================================================================
# 长回合与绑定闸门（9.1 第 10、12 条）
# ==============================================================================


@pytest.mark.asyncio
async def test_stall_window_then_deferred_delivery(tmp_path) -> None:
    """静默超过停滞窗口后改写占位消息，结果最终仍投递（9.1 第 10 条）。"""
    env = _build_env(tmp_path)
    env.gateway.push_script(FakeTurnScript(terminal_delay=0.3, final_text="迟到结果"))

    await env.send("长任务")
    assert await env.idle()

    assert cmd.DEFERRED_NOTICE_TEXT in env.adapter.texts
    assert env.adapter.texts[-1] == "迟到结果"


@pytest.mark.asyncio
async def test_stall_window_edits_placeholder_when_capable(tmp_path) -> None:
    """可原地编辑的渠道：改写占位消息，终稿也替换占位消息。"""
    capabilities = ChannelCapabilities(
        can_edit_message=True, max_text_length=2000, max_reply_segments=None
    )
    env = _build_env(tmp_path, capabilities=capabilities)
    env.gateway.push_script(FakeTurnScript(terminal_delay=0.3, final_text="迟到结果"))

    await env.send("长任务")
    assert await env.idle()

    assert cmd.DEFERRED_NOTICE_TEXT in env.adapter.edits
    assert env.adapter.edits[-1] == "迟到结果"
    assert env.adapter.texts == [cmd.PLACEHOLDER_TEXT]


@pytest.mark.asyncio
async def test_progress_events_renew_stall_window(tmp_path) -> None:
    """停滞窗口随真实进展续期：持续有事件时不进入延迟补发。"""
    env = _build_env(tmp_path, stall_window=0.15)
    steps = [(0.05, delta_event("a")), (0.05, delta_event("b")), (0.05, delta_event("c"))]
    env.gateway.push_script(FakeTurnScript(events=steps, final_text="及时结果"))

    await env.send("长任务")
    assert await env.idle()

    assert cmd.DEFERRED_NOTICE_TEXT not in env.adapter.texts
    assert env.adapter.texts == [cmd.PLACEHOLDER_TEXT, "及时结果"]


@pytest.mark.asyncio
async def test_binding_gate_drops_stale_delivery(tmp_path) -> None:
    """等待期间开启新会话，旧回合结果不投到新会话（9.1 第 12 条）。"""
    env = _build_env(tmp_path)
    env.gateway.push_script(FakeTurnScript(terminal_delay=0.2, final_text="旧回合结果"))

    await env.send("旧回合")
    await asyncio.sleep(0.02)
    await env.send("/new")
    await env.idle()

    assert "旧回合结果" not in env.adapter.texts
    # 结果仍保留在旧会话历史（本测试用假网关模拟"已落库"）
    record = env.repository.list_routes_for_instance(env.instance_id)[0]
    assert record.active_session_id != "session-1"


@pytest.mark.asyncio
async def test_editable_channel_updates_placeholder(tmp_path) -> None:
    capabilities = ChannelCapabilities(
        can_edit_message=True, max_text_length=2000, max_reply_segments=None
    )
    env = _build_env(tmp_path, capabilities=capabilities)

    await env.send("你好")
    assert await env.idle()

    assert env.adapter.texts == [cmd.PLACEHOLDER_TEXT]
    assert env.adapter.edits == ["最终结果"]


@pytest.mark.asyncio
async def test_non_editable_channel_never_calls_edit(tmp_path) -> None:
    """能力声明未声明的能力不被使用（9.1 第 5 条的运行期体现）。"""
    env = _build_env(tmp_path)

    await env.send("你好")
    assert await env.idle()

    assert env.adapter.edits == []


# ==============================================================================
# 失败与降级
# ==============================================================================


@pytest.mark.asyncio
async def test_turn_error_gets_explicit_reply(tmp_path) -> None:
    env = _build_env(tmp_path)
    env.gateway.push_script(
        FakeTurnScript(emit_terminal=False, events=[(0.0, error_event("STREAM_ERROR"))])
    )

    await env.send("你好")
    assert await env.idle()

    assert env.adapter.texts[-1] == cmd.TURN_FAILED_TEXT


@pytest.mark.asyncio
async def test_resolve_failure_gets_explicit_reply(tmp_path) -> None:
    env = _build_env(tmp_path)
    env.gateway.resolve_error = err.channel_agent_not_runnable(
        agent_id="agent-1", status="error", agent_name="demo-agent"
    )

    await env.send("你好")
    assert await env.idle()

    assert "demo-agent" in env.adapter.texts[-1]


@pytest.mark.asyncio
async def test_uncertain_placeholder_is_not_resent(tmp_path) -> None:
    """结果不确定 -> 不重发（否则用户会看到两条占位消息，9.1 的成对降级测试）。"""
    env = _build_env(
        tmp_path,
        send_results=[DeliveryResult.uncertain_with(err.CHANNEL_DELIVERY_UNCERTAIN)],
    )

    await env.send("你好")
    assert await env.idle()

    assert env.adapter.texts.count(cmd.PLACEHOLDER_TEXT) == 1
    # 占位失败不阻断回合：终稿仍然投递
    assert "最终结果" in env.adapter.texts


@pytest.mark.asyncio
async def test_placeholder_delivered_without_ref_is_not_reported_as_failed(
    tmp_path, caplog
) -> None:
    """回执不带消息标识 ≠ 投递失败（真机 2026-09-13 企微占位消息的误导性告警）。

    企微 `aibot_send_msg` 的回执没有 msgid，投递明明是 `delivered`，日志却报
    "Channel placeholder delivery failed"——排障时会被带偏，且把 `placeholder_ref`
    置空会掩盖"已送达但平台不支持原地改写"这一真实状态。
    """
    env = _build_env(tmp_path, send_results=[DeliveryResult.delivered_with("")])

    with caplog.at_level(logging.WARNING, logger="witty_service.channels.router"):
        await env.send("你好")
        assert await env.idle()

    assert env.adapter.texts.count(cmd.PLACEHOLDER_TEXT) == 1
    assert "placeholder delivery failed" not in caplog.text
    assert "最终结果" in env.adapter.texts


@pytest.mark.asyncio
async def test_uncertain_final_segment_stops_remaining_segments(tmp_path) -> None:
    """分段失败即停：第 1 段结果不确定时不再发送后续段。"""
    capabilities = ChannelCapabilities(
        can_edit_message=False, max_text_length=20, max_reply_segments=None
    )
    env = _build_env(
        tmp_path,
        capabilities=capabilities,
        send_results=[
            DeliveryResult.delivered_with("placeholder-1"),
            DeliveryResult.uncertain_with(err.CHANNEL_DELIVERY_UNCERTAIN),
        ],
    )
    env.gateway.push_script(
        FakeTurnScript(final_text="第一段内容。\n\n第二段内容。\n\n第三段内容。")
    )

    await env.send("你好")
    assert await env.idle()

    # 占位 + 第一段（结果不确定）之后停止，第二段不再发送
    assert len(env.adapter.texts) == 2
    assert "第三段内容。" not in env.adapter.texts


@pytest.mark.asyncio
async def test_interaction_request_is_noticed_and_rejected(tmp_path) -> None:
    """交互请求：告知用户 -> 主动拒绝 -> 不挂死（9.1 之外的防御性路径，§8.4）。"""
    env = _build_env(tmp_path)
    env.gateway.push_script(
        FakeTurnScript(events=[(0.0, question_event("req-42"))], final_text="结果")
    )

    await env.send("你好")
    assert await env.idle()

    assert cmd.INTERACTION_NOTICE_TEXT in env.adapter.texts
    assert env.gateway.rejected == [("agent-1", "session-1", "req-42")]


@pytest.mark.asyncio
async def test_delivery_records_are_written(tmp_path) -> None:
    env = _build_env(tmp_path)

    await env.send("你好")
    assert await env.idle()

    records = env.repository.list_deliveries(instance_id=env.instance_id)
    assert {record.certainty for record in records} == {"delivered"}
    assert len(records) >= 2


# ==============================================================================
# 生命周期
# ==============================================================================


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight_worker(tmp_path) -> None:
    env = _build_env(tmp_path)
    env.gateway.push_script(FakeTurnScript(terminal_delay=5.0, final_text="不该到达"))

    await env.send("长任务")
    await asyncio.sleep(0.02)

    await env.router.shutdown()

    assert env.router.has_running_turn(env.route()) is False
    assert "不该到达" not in env.adapter.texts


@pytest.mark.asyncio
async def test_unregister_instance_drops_pending_state(tmp_path) -> None:
    env = _build_env(tmp_path)

    await env.send("你好")
    assert await env.idle()
    env.router.unregister_instance(env.instance_id)

    await env.send("再见")
    assert await env.idle()

    assert len(env.gateway.turns) == 1


@pytest.mark.asyncio
async def test_is_still_bound_reflects_binding(tmp_path) -> None:
    env = _build_env(tmp_path)
    await env.send("你好")
    assert await env.idle()

    route = env.route()
    assert env.router.is_still_bound(route, "session-1") is True
    assert env.router.is_still_bound(route, "session-other") is False


@pytest.mark.asyncio
async def test_inbound_without_dedup_still_works(tmp_path) -> None:
    """去重是可注入依赖：没有它时管线仍然成立（供单测与调试使用）。"""
    env = _build_env(tmp_path)
    env.router._dedup = None

    await env.send("你好", event_id="same")
    await env.send("你好", event_id="same")
    assert await env.idle()

    assert len(env.gateway.turns) == 2
    await env.router.shutdown()


@pytest.mark.asyncio
async def test_turn_failure_in_one_message_does_not_block_next(tmp_path) -> None:
    env = _build_env(tmp_path)
    env.gateway.push_script(
        FakeTurnScript(
            emit_terminal=False,
            error=DomainError(code="BOOM", message="kaboom"),
        )
    )

    await env.send("第一条")
    await env.send("第二条")
    assert await env.idle()

    assert [turn[2] for turn in env.gateway.turns] == ["第一条", "第二条"]

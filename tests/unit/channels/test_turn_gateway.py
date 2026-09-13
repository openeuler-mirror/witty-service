"""W9 回合网关的测试（用 FakeAgentManager 驱动，不依赖真实 LLM）。

覆盖实施计划点名的四条：paused 自动恢复、error 映射域错误、agent 不存在映射域错误、
`remote_runtime_agent_id` 为空时重建会话且**只提交一次**。
"""

from __future__ import annotations

import pytest

from tests.unit.channels.fakes import (
    FakeAgentManager,
    FakeTurnRepository,
    FakeTurnScript,
    delta_event,
    question_event,
)
from witty_service.channels import errors as err
from witty_service.channels.turn_gateway import AgentTurnGateway
from witty_service.domain.enums import AgentStatus
from witty_service.domain.errors import DomainError


def _gateway(
    repository: FakeTurnRepository,
    manager: FakeAgentManager,
    *,
    channel: str = "wecom_bot",
) -> AgentTurnGateway:
    return AgentTurnGateway(
        repository=repository,
        get_agent_manager=lambda _agent_id: manager,
        channel=channel,
        instance_id="instance-1",
    )


async def _collect(
    gateway: AgentTurnGateway, agent_id: str, session_id: str, text: str
) -> list[dict]:
    return [event async for event in gateway.run_turn(agent_id, session_id, text)]


# ==============================================================================
# resolve_session
# ==============================================================================


@pytest.mark.asyncio
async def test_resolve_session_creates_when_none() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    manager = FakeAgentManager(repository)
    gateway = _gateway(repository, manager)

    session_id = await gateway.resolve_session("agent-1", None)

    assert session_id == "session-1"
    assert manager.created_sessions == ["session-1"]
    # 渠道产生的会话立即写入来源标记（框架设计 §5.2）
    assert repository.origins["session-1"] == "channel:wecom_bot"


@pytest.mark.asyncio
async def test_resolve_session_reuses_usable_session() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("existing", "agent-1", remote_runtime_agent_id="rt-1")
    manager = FakeAgentManager(repository)

    session_id = await _gateway(repository, manager).resolve_session(
        "agent-1", "existing"
    )

    assert session_id == "existing"
    assert manager.created_sessions == []


@pytest.mark.asyncio
async def test_resolve_session_rebuilds_when_session_missing() -> None:
    """会话被控制台删除：自愈发生在提交之前。"""
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    manager = FakeAgentManager(repository)

    session_id = await _gateway(repository, manager).resolve_session(
        "agent-1", "deleted-session"
    )

    assert session_id == "session-1"
    assert manager.created_sessions == ["session-1"]


@pytest.mark.asyncio
async def test_resolve_session_rebuilds_when_runtime_agent_id_missing() -> None:
    """`remote_runtime_agent_id` 为空会让既有 `RUNTIME_AGENT_ID_MISSING` 必然失败。"""
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("stale", "agent-1", remote_runtime_agent_id=None)
    manager = FakeAgentManager(repository)

    session_id = await _gateway(repository, manager).resolve_session("agent-1", "stale")

    assert session_id == "session-1"
    assert manager.created_sessions == ["session-1"]


@pytest.mark.asyncio
async def test_rebuild_then_submit_happens_only_once() -> None:
    """自愈收敛到提交之前：整条链路只提交一次用户消息（§4.4）。"""
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("stale", "agent-1", remote_runtime_agent_id=None)
    manager = FakeAgentManager(repository)
    gateway = _gateway(repository, manager)

    session_id = await gateway.resolve_session("agent-1", "stale")
    await _collect(gateway, "agent-1", session_id, "帮我看看")

    assert len(manager.submitted) == 1
    assert manager.submitted[0][2] == "帮我看看"


@pytest.mark.asyncio
async def test_paused_agent_is_resumed_when_creating_session() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1", status=AgentStatus.paused)
    manager = FakeAgentManager(repository)

    session_id = await _gateway(repository, manager).resolve_session("agent-1", None)

    assert manager.resumed == ["agent-1"]
    assert session_id == "session-1"


@pytest.mark.asyncio
async def test_agent_in_error_state_is_not_runnable() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1", status=AgentStatus.error, name="坏掉的 agent")
    manager = FakeAgentManager(repository)

    with pytest.raises(DomainError) as excinfo:
        await _gateway(repository, manager).resolve_session("agent-1", None)

    assert excinfo.value.code == err.CHANNEL_AGENT_NOT_RUNNABLE
    assert excinfo.value.details["status"] == "error"
    assert manager.created_sessions == []


@pytest.mark.asyncio
async def test_missing_agent_maps_to_not_bound() -> None:
    repository = FakeTurnRepository()
    manager = FakeAgentManager(repository)

    with pytest.raises(DomainError) as excinfo:
        await _gateway(repository, manager).resolve_session("agent-missing", None)

    assert excinfo.value.code == err.CHANNEL_AGENT_NOT_BOUND


@pytest.mark.asyncio
async def test_deleted_agent_maps_to_same_not_bound_code() -> None:
    """从未绑定与绑定后被删除对用户呈现同一文案，错误码也相同。"""
    repository = FakeTurnRepository()
    repository.add_agent("agent-1", status=AgentStatus.deleted)
    manager = FakeAgentManager(repository)

    with pytest.raises(DomainError) as excinfo:
        await _gateway(repository, manager).resolve_session("agent-1", None)

    assert excinfo.value.code == err.CHANNEL_AGENT_NOT_BOUND


@pytest.mark.asyncio
async def test_run_turn_resumes_paused_agent() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1", status=AgentStatus.paused)
    repository.add_session("session-1", "agent-1")
    manager = FakeAgentManager(repository)
    gateway = _gateway(repository, manager)

    events = await _collect(gateway, "agent-1", "session-1", "hi")

    assert manager.resumed == ["agent-1"]
    assert events[-1]["type"] == "message.completed"


# ==============================================================================
# run_turn
# ==============================================================================


@pytest.mark.asyncio
async def test_run_turn_yields_events_until_terminal() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("session-1", "agent-1")
    manager = FakeAgentManager(
        repository,
        scripts=[
            FakeTurnScript(
                events=[(0.0, delta_event("你")), (0.0, delta_event("好"))],
                final_text="你好",
            )
        ],
    )

    events = await _collect(_gateway(repository, manager), "agent-1", "session-1", "hi")

    assert [event["type"] for event in events] == [
        "message.delta",
        "message.delta",
        "message.completed",
    ]


@pytest.mark.asyncio
async def test_run_turn_yields_error_event_and_stops() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("session-1", "agent-1")
    manager = FakeAgentManager(
        repository, scripts=[FakeTurnScript(emit_terminal=False, emit_error_event=True)]
    )

    events = await _collect(_gateway(repository, manager), "agent-1", "session-1", "hi")

    assert events[-1]["type"] == "stream.error"
    assert events[-1]["payload"]["code"] == "STREAM_ERROR"


@pytest.mark.asyncio
async def test_run_turn_raises_when_stream_ends_without_terminal() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("session-1", "agent-1")
    manager = FakeAgentManager(
        repository, scripts=[FakeTurnScript(emit_terminal=False)]
    )

    with pytest.raises(DomainError) as excinfo:
        await _collect(_gateway(repository, manager), "agent-1", "session-1", "hi")

    assert excinfo.value.code == err.CHANNEL_TURN_FAILED


@pytest.mark.asyncio
async def test_aborted_session_maps_to_turn_aborted() -> None:
    """/stop 之后流会无声结束：这一条不能回错误文案。"""
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("session-1", "agent-1")
    repository.set_last_assistant("session-1", "半截答案", status="interrupted")
    manager = FakeAgentManager(
        repository, scripts=[FakeTurnScript(emit_terminal=False)]
    )

    with pytest.raises(DomainError) as excinfo:
        await _collect(_gateway(repository, manager), "agent-1", "session-1", "hi")

    assert excinfo.value.code == err.CHANNEL_TURN_ABORTED


@pytest.mark.asyncio
async def test_manager_domain_error_is_mapped() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("session-1", "agent-1")
    manager = FakeAgentManager(
        repository,
        scripts=[
            FakeTurnScript(
                error=DomainError(code="AGENT_NOT_RUNNING", message="not running")
            )
        ],
    )

    with pytest.raises(DomainError) as excinfo:
        await _collect(_gateway(repository, manager), "agent-1", "session-1", "hi")

    assert excinfo.value.code == err.CHANNEL_AGENT_NOT_RUNNABLE


@pytest.mark.asyncio
async def test_question_event_is_passed_through() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("session-1", "agent-1")
    manager = FakeAgentManager(
        repository,
        scripts=[FakeTurnScript(events=[(0.0, question_event("req-9"))])],
    )

    events = await _collect(_gateway(repository, manager), "agent-1", "session-1", "hi")

    assert events[0]["type"] == "question.asked"
    assert events[0]["payload"]["request_id"] == "req-9"


# ==============================================================================
# 其余窄接口
# ==============================================================================


@pytest.mark.asyncio
async def test_last_assistant_text_reads_repository() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_session("session-1", "agent-1")
    repository.set_last_assistant("session-1", "落库的最终结果")
    gateway = _gateway(repository, FakeAgentManager(repository))

    assert gateway.last_assistant_text("session-1") == "落库的最终结果"
    assert gateway.last_assistant_text("missing") is None


@pytest.mark.asyncio
async def test_last_assistant_text_ignores_blank_content() -> None:
    repository = FakeTurnRepository()
    repository.set_last_assistant("session-1", "   ")

    assert _gateway(repository, FakeAgentManager(repository)).last_assistant_text(
        "session-1"
    ) is None


@pytest.mark.asyncio
async def test_abort_and_reject_delegate_to_manager() -> None:
    repository = FakeTurnRepository()
    manager = FakeAgentManager(repository)
    gateway = _gateway(repository, manager)

    await gateway.abort("agent-1", "session-1")
    await gateway.reject_interaction("agent-1", "session-1", "req-1")

    assert manager.aborted == [("agent-1", "session-1")]
    assert manager.rejected == [("agent-1", "session-1", "req-1")]


def test_agent_state_labels() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    repository.add_agent("agent-2", status=AgentStatus.deleted)
    gateway = _gateway(repository, FakeAgentManager(repository))

    assert gateway.agent_state(None) == "unbound"
    assert gateway.agent_state("") == "unbound"
    assert gateway.agent_state("agent-1") == "running"
    assert gateway.agent_state("agent-2") == "deleted"
    assert gateway.agent_state("missing") == "deleted"


def test_session_origin_prefix() -> None:
    gateway = _gateway(FakeTurnRepository(), FakeAgentManager(FakeTurnRepository()))

    assert gateway.session_origin == "channel:wecom_bot"
    # 按实例传渠道：同一个网关服务多个渠道实例时，来源标记取实际实例的渠道
    assert gateway.origin_for("feishu_bot") == "channel:feishu_bot"
    assert gateway.origin_for(None) == "channel:wecom_bot"


@pytest.mark.asyncio
async def test_resolve_session_marks_origin_of_the_calling_instance() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1")
    gateway = _gateway(repository, FakeAgentManager(repository))

    session_id = await gateway.resolve_session("agent-1", None, channel="feishu_bot")

    assert repository.origins[session_id] == "channel:feishu_bot"


def test_agent_name_labels() -> None:
    repository = FakeTurnRepository()
    repository.add_agent("agent-1", name="演示 agent")
    repository.add_agent("agent-2", status=AgentStatus.deleted)
    gateway = _gateway(repository, FakeAgentManager(repository))

    assert gateway.agent_name(None) is None
    assert gateway.agent_name("agent-1") == "演示 agent"
    assert gateway.agent_name("agent-2") is None  # 已删除
    assert gateway.agent_name("missing") is None


def test_session_title_reader() -> None:
    repository = FakeTurnRepository()
    repository.add_session("session-1", "agent-1", title="关于构建失败")
    repository.add_session("session-2", "agent-1")
    gateway = _gateway(repository, FakeAgentManager(repository))

    assert gateway.session_title("session-1") == "关于构建失败"
    assert gateway.session_title("session-2") is None
    assert gateway.session_title("missing") is None
    assert gateway.session_title(None) is None

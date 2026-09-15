"""渠道层测试替身（框架设计 §10.1：测试替身随测试发布，不进主包）。

包含：

- `FakeAdapter`：契约与路由测试用的假渠道，记录每一次出站并可按脚本返回三态；
- `FakeTurnGateway` / `FakeAgentManager`：假 agent，不依赖真实 LLM 与真实平台；
- `FakeTurnRepository`：回合网关所需的最小仓储替身。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from witty_service.channels.contracts import (
    ChannelCapabilities,
    DeliveryResult,
    InboundMessage,
    Route,
    TurnEvent,
)
from witty_service.domain.enums import AgentStatus

# ==============================================================================
# 假仓储
# ==============================================================================


@dataclass
class FakeAgent:
    id: str
    name: str = "demo-agent"
    status: AgentStatus = AgentStatus.running


@dataclass
class FakeSession:
    id: str
    agent_id: str
    status: str = "idle"
    remote_runtime_agent_id: str | None = "runtime-agent-1"
    title: str | None = None


@dataclass
class FakeMessage:
    id: str
    session_id: str
    content: str
    status: str = "completed"


class FakeTurnRepository:
    """`AgentTurnGateway` 需要的最小仓储替身。"""

    def __init__(self) -> None:
        self.agents: dict[str, FakeAgent] = {}
        self.sessions: dict[str, FakeSession] = {}
        self.messages: dict[str, list[FakeMessage]] = {}
        self.origins: dict[str, str] = {}

    def add_agent(self, agent_id: str, **kwargs: Any) -> FakeAgent:
        agent = FakeAgent(id=agent_id, **kwargs)
        self.agents[agent_id] = agent
        return agent

    def add_session(self, session_id: str, agent_id: str, **kwargs: Any) -> FakeSession:
        session = FakeSession(id=session_id, agent_id=agent_id, **kwargs)
        self.sessions[session_id] = session
        return session

    def set_last_assistant(
        self, session_id: str, content: str, *, status: str = "completed"
    ) -> FakeMessage:
        message = FakeMessage(
            id=f"msg-{session_id}", session_id=session_id, content=content, status=status
        )
        self.messages[session_id] = [message]
        return message

    def get_agent(self, agent_id: str) -> FakeAgent | None:
        return self.agents.get(agent_id)

    def get_session(self, session_id: str) -> FakeSession | None:
        return self.sessions.get(session_id)

    def find_last_assistant_message_for_session(
        self, session_id: str
    ) -> FakeMessage | None:
        messages = self.messages.get(session_id) or []
        return messages[-1] if messages else None

    def mark_session_origin(self, session_id: str, origin: str) -> FakeSession:
        self.origins[session_id] = origin
        return self.sessions[session_id]


# ==============================================================================
# 假 agent（真实 AgentManager 的替身）
# ==============================================================================


@dataclass
class FakeTurnScript:
    """一次回合的脚本。

    `events` 是 `(延迟秒数, 事件)` 的序列；`terminal_delay` 是终态之前的静默时长
    （用于驱动停滞窗口）；`error` 在事件之后抛出；`final_text=None` 表示不发终态。
    """

    events: list[tuple[float, TurnEvent]] = field(default_factory=list)
    final_text: str | None = "最终结果"
    terminal_delay: float = 0.0
    error: BaseException | None = None
    emit_terminal: bool = True
    emit_error_event: bool = False


def terminal_event(text: str | None, *, event_type: str = "message.completed") -> TurnEvent:
    return {"type": event_type, "payload": {"text": text or ""}}


def delta_event(text: str) -> TurnEvent:
    return {"type": "message.delta", "payload": {"delta": text}}


def error_event(code: str, message: str = "boom") -> TurnEvent:
    return {"type": "stream.error", "payload": {"code": code, "message": message}}


def question_event(request_id: str = "req-1") -> TurnEvent:
    return {"type": "question.asked", "payload": {"request_id": request_id}}


class FakeAgentManager:
    """`ChannelAgentManager` 的替身：记录调用，按脚本产出事件。"""

    def __init__(
        self,
        repository: FakeTurnRepository,
        *,
        scripts: list[FakeTurnScript] | None = None,
    ) -> None:
        self.repository = repository
        self.scripts: list[FakeTurnScript] = list(scripts or [])
        self.default_script = FakeTurnScript()
        self.created_sessions: list[str] = []
        self.submitted: list[tuple[str, str, str]] = []
        self.aborted: list[tuple[str, str]] = []
        self.rejected: list[tuple[str, str, str]] = []
        self.resumed: list[str] = []
        self._session_counter = 0

    def push_script(self, script: FakeTurnScript) -> None:
        self.scripts.append(script)

    async def create_session(
        self, agent_id: str, runtime_agent_id: str | None = None
    ) -> FakeSession:
        self._session_counter += 1
        session_id = f"session-{self._session_counter}"
        session = self.repository.add_session(
            session_id, agent_id, remote_runtime_agent_id=runtime_agent_id or "runtime-agent-1"
        )
        self.created_sessions.append(session_id)
        return session

    async def resume_agent(self, agent_id: str) -> FakeAgent:
        self.resumed.append(agent_id)
        agent = self.repository.agents[agent_id]
        agent.status = AgentStatus.running
        return agent

    async def send_message_stream(
        self, agent_id: str, session_id: str, content: str
    ) -> AsyncIterator[dict[str, Any]]:
        self.submitted.append((agent_id, session_id, content))
        script = self.scripts.pop(0) if self.scripts else self.default_script
        for delay, event in script.events:
            if delay:
                await asyncio.sleep(delay)
            yield {"sandbox_type": "local_process", "event": event}
        if script.terminal_delay:
            await asyncio.sleep(script.terminal_delay)
        if script.error is not None:
            raise script.error
        if script.emit_error_event:
            yield {
                "sandbox_type": "local_process",
                "event": error_event("STREAM_ERROR", "upstream failed"),
            }
            return
        if script.emit_terminal:
            yield {
                "sandbox_type": "local_process",
                "event": terminal_event(script.final_text),
            }

    async def abort_session(
        self,
        agent_id: str,
        session_id: str,
        runtime_agent_id: str | None = None,
    ) -> dict[str, object]:
        self.aborted.append((agent_id, session_id))
        return {"id": session_id, "aborted": True}

    async def reject_question(
        self, *, agent_id: str, session_id: str, request_id: str
    ) -> None:
        self.rejected.append((agent_id, session_id, request_id))


# ==============================================================================
# 假回合网关（SessionRouter 的依赖）
# ==============================================================================


class FakeTurnGateway:
    """`RouterTurnGateway` 的替身。"""

    def __init__(
        self,
        *,
        agent_states: dict[str, str] | None = None,
        agent_names: dict[str, str] | None = None,
        session_titles: dict[str, str] | None = None,
        rebuild_always: bool = False,
    ) -> None:
        self.agent_states = agent_states or {}
        self.agent_names = agent_names or {}
        self.session_titles = session_titles or {}
        self.rebuild_always = rebuild_always
        self.scripts: list[FakeTurnScript] = []
        self.resolved: list[tuple[str, str | None]] = []
        #: 每次 resolve_session 收到的 (channel, instance_id)：按实例取来源标记
        self.resolve_channels: list[tuple[str | None, str | None]] = []
        self.turns: list[tuple[str, str, str]] = []
        self.aborted: list[tuple[str, str]] = []
        self.rejected: list[tuple[str, str, str]] = []
        self.last_texts: dict[str, str] = {}
        self.resolve_error: Exception | None = None
        self.known_sessions: dict[str, str] = {}
        self._session_counter = 0
        self.default_script = FakeTurnScript()

    def push_script(self, script: FakeTurnScript) -> None:
        self.scripts.append(script)

    async def resolve_session(
        self,
        agent_id: str,
        session_id: str | None,
        *,
        channel: str | None = None,
        instance_id: str | None = None,
    ) -> str:
        self.resolved.append((agent_id, session_id))
        self.resolve_channels.append((channel, instance_id))
        if self.resolve_error is not None:
            raise self.resolve_error
        if (
            session_id is not None
            and not self.rebuild_always
            and self.known_sessions.get(session_id) == agent_id
        ):
            return session_id
        self._session_counter += 1
        new_session_id = f"session-{self._session_counter}"
        self.known_sessions[new_session_id] = agent_id
        return new_session_id

    async def run_turn(
        self, agent_id: str, session_id: str, text: str
    ) -> AsyncIterator[TurnEvent]:
        self.turns.append((agent_id, session_id, text))
        script = self.scripts.pop(0) if self.scripts else self.default_script
        for delay, event in script.events:
            if delay:
                await asyncio.sleep(delay)
            yield event
        if script.terminal_delay:
            await asyncio.sleep(script.terminal_delay)
        if script.error is not None:
            raise script.error
        if script.emit_error_event:
            yield error_event("STREAM_ERROR", "upstream failed")
            return
        if script.emit_terminal:
            yield terminal_event(script.final_text)

    async def abort(self, agent_id: str, session_id: str) -> None:
        self.aborted.append((agent_id, session_id))

    async def reject_interaction(
        self, agent_id: str, session_id: str, request_id: str
    ) -> None:
        self.rejected.append((agent_id, session_id, request_id))

    def last_assistant_text(self, session_id: str) -> str | None:
        return self.last_texts.get(session_id)

    def agent_state(self, agent_id: str | None) -> str:
        if not agent_id:
            return "unbound"
        return self.agent_states.get(agent_id, "running")

    def agent_name(self, agent_id: str | None) -> str | None:
        if not agent_id:
            return None
        return self.agent_names.get(agent_id, "demo-agent")

    def session_title(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        return self.session_titles.get(session_id)


# ==============================================================================
# 假适配器
# ==============================================================================


@dataclass
class SentCall:
    kind: str  # "text" | "edit"
    route: Route
    text: str
    message_ref: str | None = None


class FakeAdapter:
    """契约与路由测试使用的假渠道：记录出站，按脚本返回三态。"""

    channel = "fake_bot"
    adapter_version = "test-1.0"

    def __init__(
        self,
        *,
        capabilities: ChannelCapabilities | None = None,
        send_results: list[DeliveryResult] | None = None,
        edit_results: list[DeliveryResult] | None = None,
        default_send_result: DeliveryResult | None = None,
    ) -> None:
        self.channel_capabilities = capabilities or ChannelCapabilities(
            can_edit_message=False, max_text_length=2000, max_reply_segments=None
        )
        self.send_results = list(send_results or [])
        self.edit_results = list(edit_results or [])
        self.default_send_result = default_send_result
        self.calls: list[SentCall] = []
        self.started = 0
        self.stopped = 0
        self.handler: Callable[[InboundMessage], Awaitable[None]] | None = None

    # --- ChannelAdapter ---
    async def start(self) -> None:
        self.started += 1

    def is_alive(self) -> bool:
        return self.started > self.stopped

    async def stop(self) -> None:
        self.stopped += 1

    def capabilities(self) -> ChannelCapabilities:
        return self.channel_capabilities

    async def send_text(self, route: Route, text: str) -> DeliveryResult:
        self.calls.append(SentCall(kind="text", route=route, text=text))
        return self._next_result(self.send_results, f"msg-{len(self.calls)}")

    async def edit_text(
        self, route: Route, message_ref: str, text: str
    ) -> DeliveryResult:
        self.calls.append(
            SentCall(kind="edit", route=route, text=text, message_ref=message_ref)
        )
        return self._next_result(self.edit_results, None)

    def on_inbound(
        self, handler: Callable[[InboundMessage], Awaitable[None]]
    ) -> None:
        self.handler = handler

    # --- 测试辅助 ---
    def _next_result(
        self, queue: list[DeliveryResult], auto_ref: str | None
    ) -> DeliveryResult:
        if queue:
            return queue.pop(0)
        if self.default_send_result is not None and auto_ref is not None:
            return self.default_send_result
        if auto_ref is None:
            return DeliveryResult.delivered_with("edited-1")
        return DeliveryResult.delivered_with(auto_ref)

    @property
    def texts(self) -> list[str]:
        return [call.text for call in self.calls if call.kind == "text"]

    @property
    def edits(self) -> list[str]:
        return [call.text for call in self.calls if call.kind == "edit"]

    def sent_texts_for(self, route: Route) -> list[str]:
        return [
            call.text
            for call in self.calls
            if call.kind == "text" and call.route == route
        ]

    async def emit(self, message: InboundMessage) -> None:
        assert self.handler is not None, "on_inbound was not registered"
        await self.handler(message)


def now() -> datetime:
    return datetime.now(UTC)

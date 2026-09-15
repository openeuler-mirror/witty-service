"""回合网关：调用既有 `AgentManager` 的进程内窄接口（框架设计 §3.5、ADR-0001）。

渠道层**只**通过本模块触碰 Agent / Session 能力，不依赖 `adapter/` 的 WebSocket
细节，也不修改 `AgentManager` 的任何语义。

本模块负责四件事：

1. `paused` 时自动恢复；
2. 非 `running` 时映射为域错误（`CHANNEL_AGENT_NOT_RUNNABLE` / `CHANNEL_AGENT_NOT_BOUND`）；
3. 事件流的终态判定；
4. **提交前的自愈**（实施计划 §4.4）：`resolve_session` 在返回会话前检查"会话行是否
   存在"与"`remote_runtime_agent_id` 是否非空"，任一不满足即重建会话。这样既消除了
   框架文档 §3.6 想表达的两种失效，又**避免了在提交后重试导致的用户消息重复落库**
   （`send_message_stream` 在提交前就已把用户消息写库）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from witty_service.channels import errors as err
from witty_service.channels.contracts import (
    ERROR_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    TurnEvent,
)
from witty_service.domain.enums import AgentStatus
from witty_service.domain.errors import DomainError

logger = logging.getLogger(__name__)

#: 会话来源前缀：`channel:<渠道标识符>`（框架设计 §5.2）
CHANNEL_ORIGIN_PREFIX = "channel:"


class ChannelAgentManager(Protocol):
    """`AgentManager` 的窄接口（测试用 FakeAgentManager 实现同一组方法）。"""

    async def create_session(
        self, agent_id: str, runtime_agent_id: str | None = None
    ) -> Any: ...

    def send_message_stream(
        self, agent_id: str, session_id: str, content: str
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def abort_session(
        self,
        agent_id: str,
        session_id: str,
        runtime_agent_id: str | None = None,
    ) -> dict[str, object]: ...

    async def reject_question(
        self, *, agent_id: str, session_id: str, request_id: str
    ) -> None: ...

    async def resume_agent(self, agent_id: str) -> Any: ...


class TurnGatewayRepository(Protocol):
    """渠道回合网关需要的最小仓储接口。"""

    def get_agent(self, agent_id: str) -> Any | None: ...

    def get_session(self, session_id: str) -> Any | None: ...

    def find_last_assistant_message_for_session(self, session_id: str) -> Any | None: ...

    def mark_session_origin(self, session_id: str, origin: str) -> Any: ...


class AgentTurnGateway:
    """渠道层的回合入口。"""

    def __init__(
        self,
        *,
        repository: TurnGatewayRepository,
        get_agent_manager: Callable[[str], ChannelAgentManager],
        channel: str,
        instance_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._get_agent_manager = get_agent_manager
        self._channel = channel
        self._instance_id = instance_id or ""

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def session_origin(self) -> str:
        return self.origin_for()

    # ==========================================================================
    # 会话解析（含提交前自愈）
    # ==========================================================================

    async def resolve_session(
        self,
        agent_id: str,
        session_id: str | None,
        *,
        channel: str | None = None,
        instance_id: str | None = None,
    ) -> str:
        """返回可用的会话标识；不存在、已被删除或运行时标识缺失时创建新的。

        绑定关系的**写回由调用方（SessionRouter）完成**：网关只回答"该用哪个会话"。

        `channel` / `instance_id` 由调用方按**当前渠道实例**传入：会话来源标记与错误
        详情都取实际实例的值，因此同一个回合网关可以服务多个渠道实例（框架设计 §5.2）。
        """
        agent = self._require_agent(agent_id, instance_id=instance_id)
        if session_id is not None and self._is_session_usable(agent_id, session_id):
            return session_id
        if session_id is not None:
            logger.info(
                "Channel session is stale, rebuilding: agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
        return await self._create_session(
            agent_id, agent, origin=self.origin_for(channel)
        )

    def _is_session_usable(self, agent_id: str, session_id: str) -> bool:
        """两次提交前检查：会话行存在 + `remote_runtime_agent_id` 非空（§4.4）。"""
        session = self._repository.get_session(session_id)
        if session is None:
            return False
        if getattr(session, "agent_id", None) != agent_id:
            return False
        return bool(getattr(session, "remote_runtime_agent_id", None))

    def origin_for(self, channel: str | None = None) -> str:
        """会话来源标记：`channel:<渠道标识符>`（缺省取本网关构造时的渠道）。"""
        return f"{CHANNEL_ORIGIN_PREFIX}{channel or self._channel}"

    async def _create_session(self, agent_id: str, agent: Any, *, origin: str) -> str:
        agent = await self._ensure_runnable(agent_id, agent)
        manager = self._get_agent_manager(agent_id)
        session = await manager.create_session(agent_id)
        session_id = str(session.id)
        try:
            self._repository.mark_session_origin(session_id, origin)
        except Exception:
            # 来源标记是治理信息，失败不应阻断回合
            logger.warning(
                "Failed to mark channel session origin: session_id=%s",
                session_id,
                exc_info=True,
            )
        return session_id

    # ==========================================================================
    # 回合
    # ==========================================================================

    async def run_turn(
        self, agent_id: str, session_id: str, text: str
    ) -> AsyncIterator[TurnEvent]:
        """提交一次回合并产出事件流，直到终态或失败事件。

        事件流的收尾约定：

        - 终态（`message.completed` / `turn.completed`）先到即终态，之后不再产出；
        - 失败（`stream.error` / `client.error`）作为**最后一个事件**产出，由调用方
          决定用户可见文案；
        - 流在没有终态也没有失败事件的情况下结束：若会话已被用户中止，抛
          `CHANNEL_TURN_ABORTED`（调用方据此不回复错误文案）；否则抛
          `CHANNEL_TURN_FAILED`。
        """
        agent = self._require_agent(agent_id)
        await self._ensure_runnable(agent_id, agent)
        manager = self._get_agent_manager(agent_id)

        completed = False
        saw_error = False
        try:
            async for chunk in manager.send_message_stream(agent_id, session_id, text):
                event = chunk.get("event") if isinstance(chunk, dict) else None
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                yield event
                if event_type in TERMINAL_EVENT_TYPES:
                    completed = True
                    break
                if event_type in ERROR_EVENT_TYPES:
                    saw_error = True
                    break
        except DomainError as exc:
            raise self._map_agent_error(exc, agent_id=agent_id, agent=agent) from exc

        if completed or saw_error:
            return
        if self._is_session_aborted(session_id):
            raise err.channel_turn_aborted(session_id=session_id)
        raise err.channel_turn_failed(session_id=session_id)

    async def abort(self, agent_id: str, session_id: str) -> None:
        manager = self._get_agent_manager(agent_id)
        await manager.abort_session(agent_id, session_id)

    async def reject_interaction(
        self, agent_id: str, session_id: str, request_id: str
    ) -> None:
        """拒绝一次中途提问；不拒绝会让回合永久等待（框架设计 §8.4）。"""
        manager = self._get_agent_manager(agent_id)
        await manager.reject_question(
            agent_id=agent_id, session_id=session_id, request_id=request_id
        )

    def last_assistant_text(self, session_id: str) -> str | None:
        """读取该会话已落库的最后一条 assistant 文本（终态结果的事实源）。"""
        message = self._repository.find_last_assistant_message_for_session(session_id)
        if message is None:
            return None
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        return None

    # ==========================================================================
    # agent 状态
    # ==========================================================================

    def agent_state(self, agent_id: str | None) -> str:
        """`/status` 与实例接口共用的 agent 状态标签：`unbound` / `deleted` / 状态值。"""
        if not agent_id:
            return "unbound"
        agent = self._repository.get_agent(agent_id)
        if agent is None:
            return "deleted"
        status = self._status_value(agent)
        if status == AgentStatus.deleted.value:
            return "deleted"
        return status

    def agent_name(self, agent_id: str | None) -> str | None:
        """agent 展示名（用户可见文案里用）；未绑定或已删除时返回 None。"""
        if not agent_id:
            return None
        agent = self._repository.get_agent(agent_id)
        if agent is None or self._status_value(agent) == AgentStatus.deleted.value:
            return None
        name = getattr(agent, "name", None)
        return name if isinstance(name, str) and name else None

    def session_title(self, session_id: str | None) -> str | None:
        """会话标题（`/status` 展示用）；会话不存在或没有标题时返回 None。"""
        if not session_id:
            return None
        session = self._repository.get_session(session_id)
        if session is None:
            return None
        title = getattr(session, "title", None)
        return title if isinstance(title, str) and title.strip() else None

    def _require_agent(self, agent_id: str, *, instance_id: str | None = None) -> Any:
        agent = self._repository.get_agent(agent_id)
        if agent is None or self._status_value(agent) == AgentStatus.deleted.value:
            # "从未绑定"与"绑定后被删除"对用户呈现同一文案，差异靠日志与实例详情区分
            raise err.channel_agent_not_bound(
                instance_id=instance_id or self._instance_id, agent_id=agent_id
            )
        return agent

    async def _ensure_runnable(self, agent_id: str, agent: Any) -> Any:
        status = self._status_value(agent)
        if status == AgentStatus.paused.value:
            manager = self._get_agent_manager(agent_id)
            agent = await manager.resume_agent(agent_id)
            status = self._status_value(agent)
        if status != AgentStatus.running.value:
            raise err.channel_agent_not_runnable(
                agent_id=agent_id,
                status=status,
                agent_name=getattr(agent, "name", None),
            )
        return agent

    def _map_agent_error(
        self, exc: DomainError, *, agent_id: str, agent: Any
    ) -> DomainError:
        """把既有 `AgentManager` 的错误码映射为渠道层错误码。"""
        if exc.code in {"AGENT_NOT_FOUND", "AGENT_NOT_RUNNING"}:
            if exc.code == "AGENT_NOT_FOUND":
                return err.channel_agent_not_bound(
                    instance_id=self._instance_id, agent_id=agent_id
                )
            return err.channel_agent_not_runnable(
                agent_id=agent_id,
                status=self._status_value(agent),
                agent_name=getattr(agent, "name", None),
            )
        return exc

    def _is_session_aborted(self, session_id: str) -> bool:
        """会话是否已被中止：abort 会把最后一条 assistant 消息标记为 interrupted。"""
        message = self._repository.find_last_assistant_message_for_session(session_id)
        status = getattr(message, "status", None)
        value = getattr(status, "value", status)
        return value == "interrupted"

    @staticmethod
    def _status_value(agent: Any) -> str:
        status = getattr(agent, "status", None)
        return str(getattr(status, "value", status))


__all__ = [
    "CHANNEL_ORIGIN_PREFIX",
    "AgentTurnGateway",
    "ChannelAgentManager",
    "TurnGatewayRepository",
]

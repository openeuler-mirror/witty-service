from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any, Literal, NotRequired, TypedDict

from witty_agent_server.infra.clients.base import ClientBase

logger = logging.getLogger(__name__)


RuntimeType = Literal["openclaw", "opencode"]


class TurnEventType(StrEnum):
    """统一的运行时事件类型枚举。"""

    MESSAGE_STARTED = "message.started"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    TURN_COMPLETED = "turn.completed"
    THINKING = "thinking"
    THINKING_DELTA = "thinking.delta"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_DELTA = "tool.call.delta"
    TOOL_CALL_RESPONSE = "tool.call.response"
    SESSION_USAGE = "session.usage"
    SESSION_RUNTIME_CHANGED = "session.runtime.changed"
    STREAM_ERROR = "stream.error"
    QUESTION_ASKED = "question.asked"
    QUESTION_REPLIED = "question.replied"
    QUESTION_REJECTED = "question.rejected"


class RuntimeResult(TypedDict):
    text: str


class RuntimeChunk(TypedDict):
    type: str
    delta: NotRequired[str]


class RuntimeTurnEvent(TypedDict):
    type: str
    payload: dict[str, Any]


class RuntimeBase(ABC):
    runtime_type: RuntimeType

    def __init__(self, *, client: ClientBase | None = None) -> None:
        self._client = client
        self._turn = threading.local()

    def run_turn(
        self,
        *,
        session_key: str,
        message: str,
    ) -> Iterator[RuntimeTurnEvent]:
        """执行单轮对话并输出统一的运行时事件流。

        子类只需实现 ``_map_events()``，可按需覆盖 hook 方法：
        - ``_on_turn_begin()`` — 每轮开始时的状态初始化
        - ``_on_raw_event()`` — 每条原始事件映射前的预处理
        - ``_on_mapped_event()`` — 每条已去重的统一事件的后处理
        """
        seen_started: set[str] = set()
        last_usage: dict[str, Any] | None = None
        client = self._ensure_client()

        self._on_turn_begin(session_key, message)

        for raw in client.stream_turn(session_key=session_key, message=message):
            self._on_raw_event(raw)
            for event in self._map_events(raw):
                if self._should_skip_duplicate_started(
                    event=event, seen_started_tool_calls=seen_started
                ):
                    continue
                if self._should_skip_duplicate_usage(
                    event=event,
                    last_usage_payload=last_usage,
                ):
                    continue
                if event.get("type") == TurnEventType.SESSION_USAGE:
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        last_usage = dict(payload)

                yield from self._on_mapped_event(event)

    @abstractmethod
    def _map_events(self, raw: dict[str, Any]) -> Iterator[RuntimeTurnEvent]:
        """将客户端原始事件映射为 ``RuntimeTurnEvent``。

        每个子类实现自己的事件类型识别和 payload 提取逻辑。
        """

    def _on_turn_begin(self, session_key: str, message: str) -> None:
        """每轮开始时的状态初始化钩子（可选覆盖）。

        Per-turn 可变状态应存储在 ``self._turn``
        子类应在此钩子中初始化 ``self._turn.xxx``。
        """
        del session_key, message

    def _on_raw_event(self, raw: dict[str, Any]) -> None:
        """每条原始事件映射前的预处理钩子（可选覆盖）。"""
        del raw

    def _on_mapped_event(self, event: RuntimeTurnEvent) -> Iterator[RuntimeTurnEvent]:
        """每条已去重统一事件的后处理钩子（可选覆盖）。

        默认直接透传。子类可覆盖以实现文本累积、缺失 delta 补齐等。
        Per-turn 状态应通过 ``self._turn`` 读写。
        """
        yield event

    def create_session(self, *, session_key: str) -> None:
        """创建 runtime 侧会话。"""
        self._ensure_client().create_session(session_key=session_key)

    def delete_session(self, *, session_key: str) -> None:
        """删除 runtime 侧会话。"""
        self._ensure_client().delete_session(session_key=session_key)

    def abort_session(self, *, session_key: str) -> None:
        """终止 runtime 侧会话执行。"""
        self._ensure_client().abort_session(session_key=session_key)

    def answer_question(self, *, request_id: str, answers: list[list[str]]) -> bool:
        """回答 AI 提问。

        默认抛 NotImplementedError: 子类按需覆盖。
        """
        raise NotImplementedError(
            f"{self.runtime_type} runtime does not support question answering"
        )

    def reject_question(self, *, request_id: str) -> bool:
        """拒绝 AI 提问。

        默认抛 NotImplementedError: 子类按需覆盖。
        """
        raise NotImplementedError(
            f"{self.runtime_type} runtime does not support question rejection"
        )

    def list_sessions(self, *, agent_id: str) -> list[dict[str, Any]]:
        """列出指定 agent 在 runtime 侧可见的会话。"""
        payload = self._ensure_client().list_sessions(agent_id=agent_id)
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, list):
            logger.warning(
                "list_sessions returned invalid payload, runtime=%s agent_id=%s payload=%s",
                self.runtime_type,
                agent_id,
                payload,
            )
            return []
        logger.info(
            "list_sessions fetched from runtime, runtime=%s agent_id=%s count=%s",
            self.runtime_type,
            agent_id,
            len(sessions),
        )
        return [item for item in sessions if isinstance(item, dict)]

    def send_message(self, session_id: str, message: str) -> RuntimeResult:
        """发送一轮消息并返回最终文本结果。

        默认实现：收集所有 ``message.delta`` 文本拼接为最终结果。
        复用 ``run_turn()``，与 ``stream_message`` 走同一事件管线。
        """
        text_parts: list[str] = []
        for event in self.run_turn(session_key=session_id, message=message):
            if event.get("type") != TurnEventType.MESSAGE_DELTA:
                continue
            delta = event.get("payload", {}).get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        return RuntimeResult(text="".join(text_parts))

    def stream_message(self, session_id: str, message: str) -> Iterator[RuntimeChunk]:
        """流式发送消息并输出统一分片事件。

        默认实现：复用 ``run_turn()``，只放行 ``message.delta`` 事件，
        遇 ``message.completed`` / ``turn.completed`` 时产出 ``done`` 终止。
        """
        for event in self.run_turn(session_key=session_id, message=message):
            event_type = event.get("type")
            if event_type == TurnEventType.MESSAGE_DELTA:
                delta = event.get("payload", {}).get("delta")
                if isinstance(delta, str) and delta:
                    yield RuntimeChunk(type="delta", delta=delta)
            elif event_type in (
                TurnEventType.MESSAGE_COMPLETED,
                TurnEventType.TURN_COMPLETED,
            ):
                yield RuntimeChunk(type="done")
                return

    @staticmethod
    def _should_skip_duplicate_started(
        *,
        event: RuntimeTurnEvent,
        seen_started_tool_calls: set[str],
    ) -> bool:
        """跳过重复的 ``tool.call.started`` 事件（按 tool_call_id 去重）。"""
        if event.get("type") != TurnEventType.TOOL_CALL_STARTED:
            return False
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return False
        tool_call_id = payload.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return False
        if tool_call_id in seen_started_tool_calls:
            return True
        seen_started_tool_calls.add(tool_call_id)
        return False

    @staticmethod
    def _should_skip_duplicate_usage(
        *,
        event: RuntimeTurnEvent,
        last_usage_payload: Mapping[str, Any] | None,
    ) -> bool:
        """跳过重复的 ``session.usage`` 事件（按 payload 内容相等去重）。"""
        if event.get("type") != TurnEventType.SESSION_USAGE:
            return False
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return False
        return last_usage_payload is not None and dict(last_usage_payload) == payload

    def _ensure_client(self) -> ClientBase:
        if self._client is None:
            raise RuntimeError(f"{self.runtime_type} runtime requires a client")
        return self._client


def supports_runtime_turn(runtime: object) -> bool:
    """判断 runtime 是否支持 run_turn 会话事件流能力。"""
    return callable(getattr(runtime, "run_turn", None))


def supports_runtime_lifecycle(runtime: object) -> bool:
    """判断 runtime 是否支持会话生命周期能力（create/delete/abort）。"""
    return (
        callable(getattr(runtime, "create_session", None))
        and callable(getattr(runtime, "delete_session", None))
        and callable(getattr(runtime, "abort_session", None))
    )


def supports_runtime_session_listing(runtime: object) -> bool:
    """判断 runtime 是否支持按 agent 列出会话。"""
    return callable(getattr(runtime, "list_sessions", None))

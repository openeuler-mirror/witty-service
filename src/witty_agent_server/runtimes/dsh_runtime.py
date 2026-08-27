from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from typing import Any

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.infra.clients.dsh_client import (
    DshClientError,
    derive_dsh_session_id,
)
from witty_agent_server.runtimes.runtime_base import (
    RuntimeBase,
    RuntimeTurnEvent,
    RuntimeType,
    TurnEventType,
)

logger = logging.getLogger(__name__)


class DshRuntime(RuntimeBase):
    """DeepSeek Harness（dsh）事件映射 runtime。

    输入为 ``DshClient.stream_turn`` 吐出的原始 notification
    （``{"method": ..., "payload": ...}``），映射为统一 ``RuntimeTurnEvent``。
    事件来源与结构依据 ADR-0001 P0 spike 实测样本。
    """

    runtime_type: RuntimeType = "dsh"

    def __init__(self, *, client: ClientBase | None = None) -> None:
        super().__init__(client=client)

    def run_turn(self, *, session_key: str, message: str) -> Iterator[RuntimeTurnEvent]:
        """执行单轮对话；传输层 ``DshClientError`` 转为 ``stream.error`` 事件。

        错误流经 ``_on_mapped_event`` 归一，同样遵守 started 开头、以
        ``turn.completed`` 收尾的约定：即使异常发生在任何事件产出之前
        （如 harness 未启动的 not-started），也会先补齐 ``message.started``，
        依赖终止信号的下游（如 ``stream_message`` 的 ``done`` 分片）不会悬挂。
        """
        try:
            yield from super().run_turn(session_key=session_key, message=message)
        except DshClientError as exc:
            # 经 _on_mapped_event 归一，错误流同样以 message.started 开头：
            # 若异常发生在任何事件产出之前，由 started 兜底补齐。
            yield from self._on_mapped_event(
                {
                    "type": TurnEventType.STREAM_ERROR,
                    "payload": {"code": exc.reason, "message": exc.message},
                }
            )
            yield from self._on_mapped_event(
                {
                    "type": TurnEventType.TURN_COMPLETED,
                    "payload": {"reason": "error"},
                }
            )

    def _on_turn_begin(self, session_key: str, message: str) -> None:
        del message
        # 本轮 dsh session id（与 DshClient 同一派生规则）：session 订阅含
        # 子 agent session 树，映射层按 sessionId 过滤子 session 事件。
        self._turn.dsh_session_id = derive_dsh_session_id(session_key)
        self._turn.tool_names_by_call_id = {}
        self._turn.accumulated_text = ""
        self._turn.started_emitted = False
        self._turn.completed_emitted = False

    def _map_events(self, raw: dict[str, Any]) -> Iterator[RuntimeTurnEvent]:
        yield from self._map_dsh_event(
            raw,
            session_id=self._turn.dsh_session_id,
            tool_names_by_call_id=self._turn.tool_names_by_call_id,
        )

    def _on_mapped_event(self, event: RuntimeTurnEvent) -> Iterator[RuntimeTurnEvent]:
        """统一事件约定兜底：started 开头、completed/turn.completed 收尾。"""
        event_type = event.get("type")

        if event_type == TurnEventType.MESSAGE_STARTED:
            self._turn.started_emitted = True
            yield event
            return

        if not self._turn.started_emitted:
            # 畸形流缺失 turn/start 时补齐 message.started（约定：每轮开头）。
            self._turn.started_emitted = True
            yield {"type": TurnEventType.MESSAGE_STARTED, "payload": {}}

        if event_type == TurnEventType.MESSAGE_DELTA:
            delta = event.get("payload", {}).get("delta")
            if isinstance(delta, str):
                self._turn.accumulated_text += delta
            yield event
            return

        if event_type == TurnEventType.MESSAGE_COMPLETED:
            self._turn.completed_emitted = True
            yield event
            return

        if event_type == TurnEventType.TURN_COMPLETED:
            if (
                not self._turn.completed_emitted
                and event.get("payload", {}).get("reason") != "error"
            ):
                # 无 assistant/message 的畸形轮：以累积 delta 兜底补齐
                # message.completed（约定：completed/turn.completed 收尾）。
                # error 轮不伪造 completed——错误由 stream.error 表达。
                self._turn.completed_emitted = True
                yield {
                    "type": TurnEventType.MESSAGE_COMPLETED,
                    "payload": {"text": self._turn.accumulated_text},
                }
            yield event
            return

        yield event

    # ------------------------------------------------------------------
    # 静态映射（可独立直测；P0 实测样本驱动）
    # ------------------------------------------------------------------

    @staticmethod
    def _map_dsh_event(
        raw: Mapping[str, Any],
        *,
        session_id: str | None = None,
        tool_names_by_call_id: dict[str, str] | None = None,
    ) -> Iterator[RuntimeTurnEvent]:
        """将 dsh 原始 notification 映射为 ``RuntimeTurnEvent``。

        - ``session_id`` 提供时过滤子 session（subagent 树）事件，
          为 ``None`` 时不做过滤；
        - ``tool_names_by_call_id`` 由 ``tool/call`` 事件就地填充，供
          ``tool/result``（source 仅含 callId）回填工具名。
        """
        if raw.get("method") != "session.event":
            # session.status（轮终止判定）等非 session.event 通知不映射。
            return
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            return
        if session_id is not None and payload.get("sessionId") != session_id:
            return
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            data = {}

        if event_type == "turn/start":
            yield {
                "type": TurnEventType.MESSAGE_STARTED,
                "payload": {"turn": data.get("turn")},
            }
            return

        if event_type == "turn/end":
            yield from DshRuntime._map_turn_end(data)
            return

        if event_type == "assistant/chunk":
            yield from DshRuntime._map_assistant_chunk(data)
            return

        if event_type == "assistant/message":
            yield from DshRuntime._map_assistant_message(data)
            return

        if event_type == "tool/call":
            yield from DshRuntime._map_tool_call(
                data, tool_names_by_call_id=tool_names_by_call_id
            )
            return

        if event_type == "tool/result":
            yield from DshRuntime._map_tool_result(
                data, tool_names_by_call_id=tool_names_by_call_id
            )
            return

        # step/start / step/end / user/message / session/title / request/header /
        # request/context / agent/inbox/spliced / subagent.* 等上下文、轨迹、
        # 内部事件不透出。
        logger.debug("dsh unmapped event type: %s", event_type)

    @staticmethod
    def _map_turn_end(data: Mapping[str, Any]) -> Iterator[RuntimeTurnEvent]:
        reason = data.get("reason")
        reason = reason if isinstance(reason, dict) else {}
        kind = reason.get("kind")
        if kind == "error":
            # error 时额外发 stream.error（携 reason.error 明细）；错误先发、
            # turn.completed 收尾（约定：completed/turn.completed 收尾）。
            error = reason.get("error")
            error = error if isinstance(error, dict) else {}
            yield {
                "type": TurnEventType.STREAM_ERROR,
                "payload": {
                    "code": error.get("code") or "DSH_TURN_ERROR",
                    "message": error.get("message") or "dsh turn ended with error",
                    "status": error.get("status"),
                },
            }
        yield {"type": TurnEventType.TURN_COMPLETED, "payload": {"reason": kind}}

    @staticmethod
    def _map_assistant_chunk(data: Mapping[str, Any]) -> Iterator[RuntimeTurnEvent]:
        chunk = data.get("chunk")
        if not isinstance(chunk, dict):
            return
        chunk_type = chunk.get("type")

        if chunk_type == "text-delta":
            text = chunk.get("text")
            if isinstance(text, str) and text:
                yield {"type": TurnEventType.MESSAGE_DELTA, "payload": {"delta": text}}
            return

        if chunk_type == "reasoning-delta":
            text = chunk.get("text")
            if isinstance(text, str) and text:
                yield {
                    "type": TurnEventType.THINKING_DELTA,
                    "payload": {"delta": text},
                }
            return

        if chunk_type == "block-end":
            block = chunk.get("block")
            if isinstance(block, dict) and block.get("type") == "reasoning":
                text = block.get("text")
                if isinstance(text, str) and text:
                    yield {
                        "type": TurnEventType.THINKING,
                        "payload": {"thinking": text},
                    }
            # text / tool-call 的 block-end 不透出：文本走 delta 流，
            # 工具以 tool/call 终值为准（MVP 口径）。
            return

        # block-start / tool-call-delta / usage / finish 子类型不透出：
        # usage 走 assistant/message（P0 spike-2），参数增量不透出。

    @staticmethod
    def _map_assistant_message(data: Mapping[str, Any]) -> Iterator[RuntimeTurnEvent]:
        usage = data.get("usage")
        if isinstance(usage, dict) and usage:
            yield {
                "type": TurnEventType.SESSION_USAGE,
                "payload": {"usage": dict(usage)},
            }

        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return

        # assistant/message 每个 step 一条；中间步（finish=tool-calls）含
        # tool-call 块、轮未结束，不产出 message.completed——收尾约定下
        # completed 仅由终步消息产出，缺失时由 _on_mapped_event 兜底。
        has_tool_call = any(
            isinstance(block, dict) and block.get("type") == "tool-call"
            for block in content
        )
        if has_tool_call:
            return

        # 完整 text 拼装复用 SDK final_response 逻辑（type=="text" 块拼接）。
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        yield {"type": TurnEventType.MESSAGE_COMPLETED, "payload": {"text": text}}

    @staticmethod
    def _map_tool_call(
        data: Mapping[str, Any],
        *,
        tool_names_by_call_id: dict[str, str] | None = None,
    ) -> Iterator[RuntimeTurnEvent]:
        call_id = data.get("callId")
        if not isinstance(call_id, str) or not call_id:
            return
        tool_name = data.get("name")
        if not isinstance(tool_name, str):
            tool_name = ""
        if tool_name and tool_names_by_call_id is not None:
            tool_names_by_call_id[call_id] = tool_name

        # dsh arguments 为 JSON 字符串，尽力解析为对象；失败保留原文。
        arguments: Any = data.get("arguments")
        if isinstance(arguments, str) and arguments:
            try:
                arguments = json.loads(arguments)
            except ValueError:
                logger.debug(
                    "dsh tool/call arguments is not valid JSON: callId=%s", call_id
                )

        yield {
            "type": TurnEventType.TOOL_CALL_STARTED,
            "payload": {
                "stage": "started",
                "tool_name": tool_name,
                "tool_call_id": call_id,
                "arguments": arguments,
            },
        }

    @staticmethod
    def _map_tool_result(
        data: Mapping[str, Any],
        *,
        tool_names_by_call_id: Mapping[str, str] | None = None,
    ) -> Iterator[RuntimeTurnEvent]:
        message = data.get("message")
        if not isinstance(message, dict):
            return
        source = message.get("source")
        call_id = source.get("callId") if isinstance(source, dict) else None
        if not isinstance(call_id, str) or not call_id:
            return

        # 文本在 content[].content[].text（tool-result 块内嵌 text 项拼接）；
        # isError 标记（工具失败时仍发 response）。
        content = message.get("content")
        parts: list[str] = []
        is_error = False
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool-result":
                    continue
                if block.get("isError") is True:
                    is_error = True
                nested = block.get("content")
                if not isinstance(nested, list):
                    continue
                for item in nested:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text")
                        if isinstance(text, str):
                            parts.append(text)

        # tool/result 的 source 仅含 callId，工具名从本轮 tool/call 记录回填。
        tool_name = (
            tool_names_by_call_id.get(call_id) if tool_names_by_call_id else None
        ) or "unknown"

        yield {
            "type": TurnEventType.TOOL_CALL_RESPONSE,
            "payload": {
                "stage": "response",
                "name": tool_name,
                "tool_call_id": call_id,
                "content": "".join(parts),
                "is_error": is_error,
            },
        }

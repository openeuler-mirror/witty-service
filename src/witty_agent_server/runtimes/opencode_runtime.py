from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.runtimes.runtime_base import (
    RuntimeBase,
    RuntimeTurnEvent,
    RuntimeType,
    TurnEventType,
)

logger = logging.getLogger(__name__)


class OpenCodeRuntime(RuntimeBase):
    runtime_type: RuntimeType = "opencode"

    def __init__(self, *, client: ClientBase | None = None) -> None:
        super().__init__(client=client)
        self._part_types_by_id: dict[str, str] = {}
        self._started_tool_call_ids: set[str] = set()
        self._accumulated_text: str = ""

    def _on_turn_begin(self, session_key: str, message: str) -> None:
        del session_key, message
        self._part_types_by_id = {}
        self._started_tool_call_ids = set()
        self._accumulated_text = ""

    def _on_raw_event(self, raw: dict[str, Any]) -> None:
        self._track_part_type(raw, part_types_by_id=self._part_types_by_id)

    def _map_events(self, raw: dict[str, Any]) -> Iterator[RuntimeTurnEvent]:
        yield from self._map_opencode_event(
            raw,
            part_types_by_id=self._part_types_by_id,
            started_tool_call_ids=self._started_tool_call_ids,
        )

    def _on_mapped_event(
        self, event: RuntimeTurnEvent
    ) -> Iterator[RuntimeTurnEvent]:
        if event.get("type") == TurnEventType.MESSAGE_DELTA:
            self._accumulated_text += event.get("payload", {}).get("delta", "")
        if (
            event.get("type") == TurnEventType.MESSAGE_COMPLETED
            and self._accumulated_text
        ):
            event = {
                "type": event["type"],
                "payload": {**event["payload"], "text": self._accumulated_text},
            }
        yield event

    @staticmethod
    def _map_opencode_event(
        raw: dict[str, Any],
        *,
        part_types_by_id: Mapping[str, str] | None = None,
        started_tool_call_ids: set[str] | None = None,
    ) -> Iterator[RuntimeTurnEvent]:
        """将 OpenCode SSE 原始事件映射为 ``RuntimeTurnEvent``。"""
        event_type = raw.get("type", "")

        if event_type == "message.part.updated":
            result = OpenCodeRuntime._map_part_updated(
                raw, started_tool_call_ids=started_tool_call_ids
            )
            if result is not None:
                yield result
            return

        if event_type == "message.part.delta":
            result = OpenCodeRuntime._map_part_delta(
                raw, part_types_by_id=part_types_by_id
            )
            if result is not None:
                yield result
            return

        if event_type == "message.updated":
            result = OpenCodeRuntime._map_message_updated(raw)
            if result is not None:
                yield result
            return

        if event_type == "session.status":
            result = OpenCodeRuntime._map_session_status(raw)
            if result is not None:
                yield result
            return

        if event_type == "session.idle":
            yield {"type": TurnEventType.TURN_COMPLETED, "payload": {}}
            return

        if event_type == "session.error":
            yield {"type": TurnEventType.STREAM_ERROR, "payload": {"error": raw}}
            return

        logger.debug("opencode unmapped event type: %s", event_type)

    @staticmethod
    def _map_part_updated(
        raw: dict[str, Any],
        *,
        started_tool_call_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        part = raw.get("part")
        if not isinstance(part, dict):
            return None
        part_type = part.get("type", "")

        if part_type == "text":
            # text 流式增量由 message.part.delta 事件承载；
            # message.part.updated(text) 是 part 完成后的最终完整文本，
            return None

        if part_type == "reasoning":
            # 只在 reasoning 完成且文本非空时产出 thinking 事件，
            text = part.get("text", "")
            if isinstance(text, str) and text:
                return {"type": TurnEventType.THINKING, "payload": {"thinking": text}}
            return None

        if part_type == "tool":
            state = part.get("state", "")
            if isinstance(state, dict):
                status = state.get("status", "")
            else:
                status = state

            tool_name = part.get("tool") or part.get("name", "")
            tool_call_id = part.get("callID") or part.get("id", "")

            # pending: input 总是 {}，真正的 input 要到 running 才填充，
            # 因此 pending 只做 part type 跟踪，不产出 TOOL_CALL_STARTED。
            if status == "pending":
                return None

            if status == "running":
                # 有增量输出 → tool.call.delta
                if isinstance(state, dict):
                    output = state.get("metadata", {}).get("output", "")
                    if output:
                        return {
                            "type": TurnEventType.TOOL_CALL_DELTA,
                            "payload": {
                                "stage": "delta",
                                "name": tool_name,
                                "tool_call_id": tool_call_id,
                                "content": output,
                                "status": "running",
                            },
                        }

                # 无增量输出 → tool.call.started（每个 callID 只发一次）
                if (
                    started_tool_call_ids is not None
                    and tool_call_id
                    and tool_call_id in started_tool_call_ids
                ):
                    return None
                if started_tool_call_ids is not None and tool_call_id:
                    started_tool_call_ids.add(tool_call_id)

                return {
                    "type": TurnEventType.TOOL_CALL_STARTED,
                    "payload": {
                        "stage": "started",
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": state.get("input", {}) if isinstance(state, dict) else {},
                    },
                }
            if status == "completed":
                output = state.get("output", "") if isinstance(state, dict) else ""
                exit_code = state.get("metadata", {}).get("exit") if isinstance(state, dict) else None
                result: dict[str, Any] = {
                    "type": TurnEventType.TOOL_CALL_RESPONSE,
                    "payload": {
                        "stage": "response",
                        "name": tool_name,
                        "tool_call_id": tool_call_id,
                        "content": output,
                        "is_error": False,
                    },
                }
                if exit_code is not None:
                    result["payload"]["exitCode"] = exit_code
                return result
            if status == "error":
                error_output = state.get("output", "") if isinstance(state, dict) else ""
                return {
                    "type": TurnEventType.TOOL_CALL_RESPONSE,
                    "payload": {
                        "stage": "response",
                        "name": tool_name,
                        "tool_call_id": tool_call_id,
                        "content": error_output,
                        "is_error": True,
                        "exitCode": -1,
                    },
                }
            return None

        if part_type == "step-start":
            return {"type": TurnEventType.MESSAGE_STARTED, "payload": {"part": part}}

        if part_type == "step-finish":
            usage = part.get("usage") or part.get("tokens") or {}
            cost = part.get("cost")
            payload: dict[str, Any] = {"usage": usage}
            if cost is not None:
                payload["cost"] = cost
            return {"type": TurnEventType.SESSION_USAGE, "payload": payload}

        return None

    @staticmethod
    def _map_part_delta(
        raw: dict[str, Any],
        *,
        part_types_by_id: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """映射 ``message.part.delta`` → ``message.delta`` / ``tool.call.delta`` / ``thinking.delta``。

        OpenCode text 流式增量通过独立的 ``message.part.delta`` 事件下发，

        扩展支持：
        - ``field="tool"`` → ``tool.call.delta``（工具执行增量输出）
        - ``field="reasoning"`` → ``thinking.delta``（增量思考内容）
        """
        field = raw.get("field", "")
        delta = raw.get("delta", "")
        if not delta:
            return None

        part_id = raw.get("partID")
        part_type = (
            part_types_by_id.get(part_id)
            if isinstance(part_id, str) and part_types_by_id is not None
            else None
        )
        if part_type == "reasoning":
            return {"type": TurnEventType.THINKING_DELTA, "payload": {"delta": delta}}

        if field == "text":
            return {"type": TurnEventType.MESSAGE_DELTA, "payload": {"delta": delta}}

        if field == "tool":
            return {"type": TurnEventType.TOOL_CALL_DELTA, "payload": {"delta": delta, "part": raw.get("part")}}

        return None

    @staticmethod
    def _track_part_type(
        raw: dict[str, Any], *, part_types_by_id: dict[str, str]
    ) -> None:
        if raw.get("type") != "message.part.updated":
            return
        part = raw.get("part")
        if not isinstance(part, dict):
            return
        part_id = part.get("id")
        part_type = part.get("type")
        if isinstance(part_id, str) and isinstance(part_type, str):
            part_types_by_id[part_id] = part_type

    @staticmethod
    def _map_message_updated(raw: dict[str, Any]) -> dict[str, Any] | None:
        info = raw.get("info", {})
        if isinstance(info, dict) and info.get("role") == "assistant":
            finish = info.get("finish")
            # 只有 finish == "stop" 才表示模型真正完成回复。
            # finish == "tool-calls" 表示模型决定调用工具。
            if finish == "stop":
                return {"type": TurnEventType.MESSAGE_COMPLETED, "payload": {"info": info}}
        return None

    @staticmethod
    def _map_session_status(raw: dict[str, Any]) -> dict[str, Any] | None:
        status = raw.get("status", {})
        if isinstance(status, dict) and status.get("type") == "busy":
            return {"type": TurnEventType.MESSAGE_STARTED, "payload": {"status": status}}
        return None

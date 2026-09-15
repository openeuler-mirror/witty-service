from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any, TypedDict

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.runtimes.runtime_base import (
    RuntimeBase,
    RuntimeTurnEvent,
    RuntimeType,
    TurnEventType,
    tool_call_delta_event,
)
from witty_agent_server.runtimes.usage import (
    has_token_usage,
    normalize_usage_payload,
)

logger = logging.getLogger(__name__)


class PartMeta(TypedDict, total=False):
    """``message.part.updated`` 中对增量归属有用的字段。

    ``message.part.delta`` 只带 ``partID``，而 ``tool.call.delta`` 的载荷契约要求
    ``tool_call_id``（即 part 的 ``callID``）。因此必须先在 ``message.part.updated``
    上把 ``partID → callID / 工具名`` 记下来，delta 到达时才能归属。
    """

    part_type: str
    call_id: str
    tool_name: str


def _nested_str(source: Any, *keys: str) -> str:
    """按 ``keys`` 逐层取字符串；任一层不是 dict、或最终值不是 str 时返回空串。"""
    current = source
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current if isinstance(current, str) else ""


class OpenCodeRuntime(RuntimeBase):
    runtime_type: RuntimeType = "opencode"

    def __init__(self, *, client: ClientBase | None = None) -> None:
        super().__init__(client=client)

    def _on_turn_begin(self, session_key: str, message: str) -> None:
        del session_key, message
        self._turn.parts_by_id: dict[str, PartMeta] = {}
        self._turn.started_tool_call_ids = set()
        self._turn.accumulated_text = ""

    def _on_raw_event(self, raw: dict[str, Any]) -> None:
        self._track_part(raw, parts_by_id=self._turn.parts_by_id)

    def _map_events(self, raw: dict[str, Any]) -> Iterator[RuntimeTurnEvent]:
        yield from self._map_opencode_event(
            raw,
            parts_by_id=self._turn.parts_by_id,
            started_tool_call_ids=self._turn.started_tool_call_ids,
        )

    def _on_mapped_event(self, event: RuntimeTurnEvent) -> Iterator[RuntimeTurnEvent]:
        if event.get("type") == TurnEventType.MESSAGE_DELTA:
            self._turn.accumulated_text += event.get("payload", {}).get("delta", "")
        if (
            event.get("type") == TurnEventType.MESSAGE_COMPLETED
            and self._turn.accumulated_text
        ):
            event = {
                "type": event["type"],
                "payload": {**event["payload"], "text": self._turn.accumulated_text},
            }
        yield event

    def answer_question(self, *, request_id: str, answers: list[list[str]]) -> bool:
        """回答 OpenCode 提问。

        委托给底层 client；client 不支持时由 ClientBase 默认实现抛
        NotImplementedError。
        """
        return self._ensure_client().answer_question(
            request_id=request_id, answers=answers
        )

    def reject_question(self, *, request_id: str) -> bool:
        """拒绝 OpenCode 提问。

        委托给底层 client；client 不支持时由 ClientBase 默认实现抛
        NotImplementedError。
        """
        return self._ensure_client().reject_question(request_id=request_id)

    @staticmethod
    def _map_opencode_event(
        raw: dict[str, Any],
        *,
        parts_by_id: Mapping[str, PartMeta] | None = None,
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
            result = OpenCodeRuntime._map_part_delta(raw, parts_by_id=parts_by_id)
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

        if event_type == "question.asked":
            result = OpenCodeRuntime._map_question_asked(raw)
            if result is not None:
                yield result
            else:
                yield {"type": TurnEventType.STREAM_ERROR, "payload": {"error": raw}}
            return

        if event_type == "question.replied":
            yield OpenCodeRuntime._map_question_replied(raw)
            return

        if event_type == "question.rejected":
            yield OpenCodeRuntime._map_question_rejected(raw)
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
            tool_name = part.get("tool") or part.get("name", "")

            # 此处跳过避免重复产出 question 的 tool.call.* 事件。
            if tool_name == "question":
                return None

            state = part.get("state", "")
            status = state.get("status", "") if isinstance(state, dict) else state
            tool_call_id = part.get("callID") or part.get("id", "")

            # pending: input 总是 {}，真正的 input 要到 running 才填充，
            # 因此 pending 只做 part type 跟踪，不产出 TOOL_CALL_STARTED。
            if status == "pending":
                return None

            if status == "running":
                # 每个 callID 只在首条 running 事件产一次 started；write 等工具
                # 的输入参数依赖 started（input 在 running 才填充）。即便首条
                # running 就带增量输出，也必须先发 started，否则统一 artifact
                # 钩子拿不到参数、会丢失 artifact.* 事件。
                # 已发过 started 的后续 running（带增量 output）透出为
                # tool.call.delta，前端「边跑边看」；终态输出仍由 completed /
                # error 分支的 tool.call.response 完整下发。
                if (
                    started_tool_call_ids is not None
                    and tool_call_id
                    and tool_call_id in started_tool_call_ids
                ):
                    output = _nested_str(state, "metadata", "output")
                    if not output:
                        return None
                    return tool_call_delta_event(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        delta=output,
                    )

                if started_tool_call_ids is not None and tool_call_id:
                    started_tool_call_ids.add(tool_call_id)

                return {
                    "type": TurnEventType.TOOL_CALL_STARTED,
                    "payload": {
                        "stage": "started",
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": state.get("input", {})
                        if isinstance(state, dict)
                        else {},
                    },
                }
            if status == "completed":
                output = state.get("output", "") if isinstance(state, dict) else ""
                exit_code = (
                    state.get("metadata", {}).get("exit")
                    if isinstance(state, dict)
                    else None
                )
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
                error_output = (
                    state.get("output", "") if isinstance(state, dict) else ""
                )
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
            # step-finish 带 usage（或 tokens）与同级 cost，统一归一化后下发。
            usage = normalize_usage_payload(part)
            if not has_token_usage(usage):
                return None
            return {"type": TurnEventType.SESSION_USAGE, "payload": usage}

        return None

    @staticmethod
    def _map_part_delta(
        raw: dict[str, Any],
        *,
        parts_by_id: Mapping[str, PartMeta] | None = None,
    ) -> dict[str, Any] | None:
        """映射 ``message.part.delta`` → ``message.delta`` / ``thinking.delta``
        / ``tool.call.delta``。

        OpenCode text 流式增量通过独立的 ``message.part.delta`` 事件下发，

        扩展支持：
        - ``field="tool"`` → ``tool.call.delta``（工具执行增量输出），
          归属信息取自 ``parts_by_id``（``partID → callID``）
        - ``field="reasoning"`` → ``thinking.delta``（增量思考内容）
        """
        field = raw.get("field", "")
        delta = raw.get("delta", "")
        if not delta:
            return None

        part_id = raw.get("partID")
        meta: PartMeta = (
            parts_by_id.get(part_id)
            if isinstance(part_id, str) and parts_by_id is not None
            else None
        ) or {}
        if meta.get("part_type") == "reasoning":
            return {"type": TurnEventType.THINKING_DELTA, "payload": {"delta": delta}}

        if field == "text":
            return {"type": TurnEventType.MESSAGE_DELTA, "payload": {"delta": delta}}

        if field == "tool":
            # 载荷契约要求 tool_call_id：拿不到归属就直接丢弃，不再发一条前端
            # 注定忽略的事件（见 runtime_base.tool_call_delta_event）。
            tool_call_id = meta.get("call_id", "")
            if not tool_call_id:
                return None
            return tool_call_delta_event(
                tool_call_id=tool_call_id,
                tool_name=meta.get("tool_name", ""),
                delta=delta,
            )

        return None

    @staticmethod
    def _track_part(raw: dict[str, Any], *, parts_by_id: dict[str, PartMeta]) -> None:
        """记录 part 元信息（类型 / callID / 工具名），供 ``message.part.delta`` 归属。"""
        if raw.get("type") != "message.part.updated":
            return
        part = raw.get("part")
        if not isinstance(part, dict):
            return
        part_id = part.get("id")
        if not isinstance(part_id, str) or not part_id:
            return
        meta: PartMeta = {}
        part_type = part.get("type")
        if isinstance(part_type, str) and part_type:
            meta["part_type"] = part_type
        call_id = part.get("callID")
        if isinstance(call_id, str) and call_id:
            meta["call_id"] = call_id
        tool_name = part.get("tool")
        if isinstance(tool_name, str) and tool_name:
            meta["tool_name"] = tool_name
        parts_by_id[part_id] = meta

    @staticmethod
    def _map_message_updated(raw: dict[str, Any]) -> dict[str, Any] | None:
        info = raw.get("info", {})
        if isinstance(info, dict) and info.get("role") == "assistant":
            finish = info.get("finish")
            # 只有 finish == "stop" 才表示模型真正完成回复。
            # finish == "tool-calls" 表示模型决定调用工具。
            if finish == "stop":
                return {
                    "type": TurnEventType.MESSAGE_COMPLETED,
                    "payload": {"info": info},
                }
        return None

    @staticmethod
    def _map_session_status(raw: dict[str, Any]) -> dict[str, Any] | None:
        status = raw.get("status", {})
        if isinstance(status, dict) and status.get("type") == "busy":
            return {
                "type": TurnEventType.MESSAGE_STARTED,
                "payload": {"status": status},
            }
        return None

    @staticmethod
    def _map_question_asked(raw: dict[str, Any]) -> dict[str, Any] | None:
        """映射 ``question.asked`` → ``question.asked``。

        OpenCode 事件结构::

            {
              "type": "question.asked",
              "id": "que_xxx",             # question request ID
              "sessionID": "ses_xxx",
              "questions": [QuestionInfo],
              "tool": {messageID, callID} | None,
            }
        """
        question_id = raw.get("id", "")
        if not isinstance(question_id, str) or not question_id:
            logger.warning(
                "_map_question_asked: missing or invalid question id, raw keys=%s",
                list(raw.keys()),
            )
            return None
        questions = raw.get("questions", [])
        tool = raw.get("tool")
        payload: dict[str, Any] = {
            "question_id": question_id,
            "questions": questions,
        }
        if isinstance(tool, dict):
            payload["tool"] = tool
        return {"type": TurnEventType.QUESTION_ASKED, "payload": payload}

    @staticmethod
    def _map_question_replied(raw: dict[str, Any]) -> dict[str, Any]:
        """映射 ``question.replied`` → ``question.replied``。

        OpenCode 事件结构::

            {
              "type": "question.replied",
              "sessionID": "ses_xxx",
              "requestID": "que_xxx",
              "answers": [QuestionAnswer],
            }
        """
        return {
            "type": TurnEventType.QUESTION_REPLIED,
            "payload": {
                "request_id": raw.get("requestID", ""),
                "answers": raw.get("answers", []),
            },
        }

    @staticmethod
    def _map_question_rejected(raw: dict[str, Any]) -> dict[str, Any]:
        """映射 ``question.rejected`` → ``question.rejected``。

        OpenCode 事件结构::

            {
              "type": "question.rejected",
              "sessionID": "ses_xxx",
              "requestID": "que_xxx",
            }
        """
        return {
            "type": TurnEventType.QUESTION_REJECTED,
            "payload": {
                "request_id": raw.get("requestID", ""),
            },
        }

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


class OpenClawGatewayRuntime(RuntimeBase):
    runtime_type: RuntimeType = "openclaw"

    def __init__(self, *, client: ClientBase) -> None:
        super().__init__(client=client)
        self._acc_delta: str = ""

    def _on_turn_begin(self, session_key: str, message: str) -> None:
        del session_key, message
        self._acc_delta = ""

    def _map_events(self, raw: dict[str, Any]) -> Iterator[RuntimeTurnEvent]:
        yield from self._map_gateway_events(raw)

    def _on_mapped_event(
        self, event: RuntimeTurnEvent
    ) -> Iterator[RuntimeTurnEvent]:
        # 追踪 message.delta 累积文本
        if event.get("type") == TurnEventType.MESSAGE_DELTA:
            delta = event.get("payload", {}).get("delta", "")
            if isinstance(delta, str) and delta:
                self._acc_delta += delta
        # 上游 OpenClaw runtime 可能将最后几个字符直接打包到 session.message
        # 的完整文本中，而未通过 assistant stream 以 delta 形式下发。此处检测
        # message.completed 文本是否比累积 delta 更长，补发缺失的末尾 delta。
        elif event.get("type") == TurnEventType.MESSAGE_COMPLETED:
            full_text = event.get("payload", {}).get("text", "")
            if (
                isinstance(full_text, str)
                and len(full_text) > len(self._acc_delta)
                and full_text.startswith(self._acc_delta)
            ):
                missing = full_text[len(self._acc_delta):]
                if missing:
                    yield {
                        "type": TurnEventType.MESSAGE_DELTA,
                        "payload": {"delta": missing},
                    }
                    self._acc_delta = full_text
        yield event


    def _map_gateway_events(
        self, raw_event: Mapping[str, Any]
    ) -> Iterator[RuntimeTurnEvent]:
        raw_type = raw_event.get("type")
        payload = raw_event.get("payload")
        if not isinstance(raw_type, str):
            return
        normalized_payload: dict[str, Any] = (
            payload if isinstance(payload, dict) else {}
        )

        if raw_type == "session.usage":
            if normalized_payload:
                yield {"type": TurnEventType.SESSION_USAGE, "payload": normalized_payload}
            return

        if raw_type == "sessions.changed":
            runtime_session_id = self._pick_string(normalized_payload, "sessionId")
            if runtime_session_id is None:
                data = normalized_payload.get("data")
                if isinstance(data, dict):
                    runtime_session_id = self._pick_string(data, "sessionId")
            if runtime_session_id is None:
                session = normalized_payload.get("session")
                if isinstance(session, dict):
                    runtime_session_id = self._pick_string(session, "sessionId")
            if runtime_session_id is None:
                return
            yield {
                "type": TurnEventType.SESSION_RUNTIME_CHANGED,
                "payload": {
                    "runtime_session_id": runtime_session_id,
                },
            }
            return

        if raw_type == "agent":
            stream = normalized_payload.get("stream")
            if not isinstance(stream, str):
                return
            data = normalized_payload.get("data")
            if not isinstance(data, dict):
                return
            if stream == "assistant":
                delta = data.get("delta")
                if isinstance(delta, str) and delta:
                    yield {"type": TurnEventType.MESSAGE_DELTA, "payload": {"delta": delta}}
                return
            if stream == "thinking":
                delta = data.get("delta")
                text = data.get("text")
                if isinstance(delta, str) and delta:
                    yield {
                        "type": TurnEventType.THINKING_DELTA,
                        "payload": {
                            "delta": delta,
                            "text": text if isinstance(text, str) else None,
                        },
                    }
                return
            if stream == "tool":
                yield from self._map_agent_tool_stream(data)
                return
            if stream == "sessions.usage":
                usage_payload = self._extract_usage_payload(data)
                if usage_payload is not None:
                    yield {"type": TurnEventType.SESSION_USAGE, "payload": usage_payload}
                return
            if stream == "lifecycle":
                phase = self._pick_string(data, "phase")
                if not isinstance(phase, str):
                    return
                normalized_phase = phase.lower()
                if normalized_phase == "start":
                    yield {"type": TurnEventType.MESSAGE_STARTED, "payload": {}}
                    return
                if normalized_phase == "end":
                    yield {"type": TurnEventType.TURN_COMPLETED, "payload": {}}
                    return
                if normalized_phase == "error":
                    code = self._pick_string(data, "code") or "OPENCLAW_LIFECYCLE_ERROR"
                    message = (
                        self._pick_string(
                            data,
                            "message",
                            "error",
                        )
                        or "openclaw lifecycle stream error"
                    )
                    yield {
                        "type": TurnEventType.STREAM_ERROR,
                        "payload": {
                            "code": code,
                            "message": message,
                            "source": "lifecycle",
                        },
                    }
                    return
            return

        if raw_type == "session.message":
            nested = normalized_payload.get("message")
            if not isinstance(nested, dict):
                return
            yield from self._map_session_message(nested)
            return
        return

    def _map_session_message(
            self, message: Mapping[str, Any]
        ) -> Iterator[RuntimeTurnEvent]:
            role = message.get("role")
            content = message.get("content")
            if role == "toolResult":
                yield from self._map_tool_result_message(message)
                return
            if role == "assistant":
                if message.get("stopReason") != "stop":
                    yield from self._extract_thinking_events(message)
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict) or item.get("type") != "toolCall":
                            continue
                        yield {
                            "type": TurnEventType.TOOL_CALL_STARTED,
                            "payload": {
                                "stage": "started",
                                "tool_name": self._pick_string(item, "name") or "unknown",
                                "tool_call_id": self._pick_string(item, "id"),
                                "arguments": item.get("arguments"),
                            },
                        }
                if message.get("stopReason") != "stop":
                    return
                if isinstance(content, list):
                    text = "".join(
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict)
                        and item.get("type") == "text"
                        and isinstance(item.get("text"), str)
                    )
                    if text:
                        yield {"type": TurnEventType.MESSAGE_COMPLETED, "payload": {"text": text}}
                        return
            yield from self._extract_thinking_events(message)

    def _map_tool_result_message(
        self, message: Mapping[str, Any]
    ) -> Iterator[RuntimeTurnEvent]:
        tool_name = self._pick_string(message, "toolName", "name") or "unknown"
        tool_call_id = self._pick_string(message, "toolCallId")
        content = message.get("content", "")
        details = message.get("details")
        if not isinstance(details, dict):
            details = {}
        detail_status = details.get("status")
        is_error = self._pick_bool(message, "isError")
        yield {
            "type": TurnEventType.TOOL_CALL_RESPONSE,
            "payload": {
                "stage": "response",
                "name": tool_name,
                "tool_call_id": tool_call_id,
                "content": content,
                "is_error": bool(is_error) or detail_status == "error",
                "details": details,
                "exitCode": details.get("exitCode"),
            },
        }

    def _extract_thinking_events(
        self, message: Mapping[str, Any]
    ) -> Iterator[RuntimeTurnEvent]:
        content = message.get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "thinking":
                continue
            thinking = item.get("thinking")
            if not isinstance(thinking, str) or not thinking:
                continue
            payload: dict[str, Any] = {"thinking": thinking}
            signature = item.get("signature")
            if isinstance(signature, str) and signature:
                payload["signature"] = signature
            yield {"type": TurnEventType.THINKING, "payload": payload}

    def _map_agent_tool_stream(
        self, data: Mapping[str, Any]
    ) -> Iterator[RuntimeTurnEvent]:
        phase = data.get("phase")
        stage = phase.lower() if isinstance(phase, str) else ""

        tool_name = self._pick_string(data, "toolName", "name") or "unknown"
        tool_call_id = self._pick_string(data, "toolCallId")
        arguments = self._pick_value(data, "args")
        result = data.get("result")
        is_error = self._pick_bool(data, "isError")

        if stage == "start":
            yield {
                "type": TurnEventType.TOOL_CALL_STARTED,
                "payload": {
                    "stage": "started",
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "arguments": arguments,
                },
            }
            return

        if stage == "result":
            if not isinstance(result, dict):
                result = {}
            content = result.get("content", "")
            details = result.get("details", {})

            if not isinstance(details, dict):
                details = {}
            exitCode = details.get("exitCode", 1)
            yield {
                "type": TurnEventType.TOOL_CALL_RESPONSE,
                "payload": {
                    "stage": "response",
                    "name": tool_name,
                    "tool_call_id": tool_call_id,
                    "content": content,
                    "is_error": is_error,
                    "exitCode": exitCode,
                },
            }
            return

        # 增量更新事件: 来自 OpenClaw agent stream 的 phase:update
        # exec 运行时会通过 onUpdate 推送进程的增量 stdout/stderr
        if stage == "update":
            partial = data.get("partialResult")
            if isinstance(partial, dict):
                content = partial.get("content", "")
                details = partial.get("details", {})
            else:
                content = ""
                details = {}
            if not isinstance(details, dict):
                details = {}
            yield {
                "type": TurnEventType.TOOL_CALL_DELTA,
                "payload": {
                    "stage": "delta",
                    "name": tool_name,
                    "tool_call_id": tool_call_id,
                    "content": content,
                    "details": details,
                    "session_id": details.get("sessionId"),
                    "status": details.get("status", "running"),
                },
            }
            return

    def _pick_value(self, payload: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in payload:
                return payload[key]
        return None

    def _pick_string(self, payload: Mapping[str, Any], *keys: str) -> str | None:
        value = self._pick_value(payload, *keys)
        if isinstance(value, str) and value:
            return value
        return None

    def _pick_bool(self, payload: Mapping[str, Any], *keys: str) -> bool | None:
        value = self._pick_value(payload, *keys)
        if isinstance(value, bool):
            return value
        return None

    def _extract_usage_payload(
        self, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        seen: set[int] = set()
        queue: list[Mapping[str, Any]] = [payload]
        while queue:
            current = queue.pop(0)
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            usage = self._parse_usage_fields(current)
            if usage is not None:
                return usage
            nested_usage = current.get("usage")
            if isinstance(nested_usage, dict):
                queue.append(nested_usage)
            totals = current.get("totals")
            if isinstance(totals, dict):
                queue.append(totals)
            sessions = current.get("sessions")
            if isinstance(sessions, list):
                for session_item in sessions:
                    if not isinstance(session_item, dict):
                        continue
                    session_usage = session_item.get("usage")
                    if isinstance(session_usage, dict):
                        queue.append(session_usage)
        return None

    def _parse_usage_fields(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        output: dict[str, Any] = {}
        input_tokens = self._pick_usage_int(payload, "inputTokens")
        output_tokens = self._pick_usage_int(payload, "outputTokens")
        total_tokens = self._pick_usage_int(payload, "totalTokens")
        total_cost = self._pick_usage_float(
            payload,
            "estimatedCostUsd",
            "totalCost",
        )
        if input_tokens is not None:
            output["input_tokens"] = input_tokens
        if output_tokens is not None:
            output["output_tokens"] = output_tokens
        if total_tokens is not None:
            output["total_tokens"] = total_tokens
        if total_cost is not None:
            output["total_cost"] = total_cost
        return output if output else None

    def _pick_usage_int(self, payload: Mapping[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, int):
                return value
        return None

    def _pick_usage_float(self, payload: Mapping[str, Any], *keys: str) -> float | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

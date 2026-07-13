from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.runtimes.runtime_base import (
    RuntimeBase,
    RuntimeChunk,
    RuntimeResult,
    RuntimeTurnEvent,
    RuntimeType,
)


logger = logging.getLogger(__name__)


class OpenClawGatewayRuntime(RuntimeBase):
    runtime_type: RuntimeType = "openclaw"

    def __init__(self, *, client: ClientBase) -> None:
        self._client = client

    def list_sessions(self, *, agent_id: str) -> list[dict[str, Any]]:
        payload = self._client.list_sessions(agent_id=agent_id)
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            logger.warning(
                "list_sessions returned invalid payload, agent_id=%s payload=%s",
                agent_id,
                payload,
            )
            return []
        logger.info(
            "list_sessions fetched from runtime, agent_id=%s count=%s",
            agent_id,
            len(sessions),
        )
        return [item for item in sessions if isinstance(item, dict)]

    def create_session(self, *, session_key: str) -> None:
        self._client.create_session(session_key=session_key)

    def delete_session(self, *, session_key: str) -> None:
        self._client.delete_session(session_key=session_key)

    def abort_session(self, *, session_key: str) -> None:
        self._client.abort_session(session_key=session_key)

    # 将 OpenClaw 事件流转换为统一 RuntimeTurnEvent，供上层处理。
    def run_turn(
        self,
        *,
        session_key: str,
        message: str,
    ) -> Iterator[RuntimeTurnEvent]:
        seen_started_tool_calls: set[str] = set()
        last_usage_payload: dict[str, Any] | None = None
        _acc_delta: str = ""  # 累积已收到的 delta 文本，用于补齐上游未流出的末尾字符
        # 对接openclaw，输出原始event
        for raw_event in self._client.stream_turn(
            session_key=session_key, message=message
        ):
            for event in self._map_gateway_events(raw_event):
                if self._should_skip_duplicate_started(
                    event=event, seen_started_tool_calls=seen_started_tool_calls
                ):
                    continue
                if self._should_skip_duplicate_usage(
                    event=event,
                    last_usage_payload=last_usage_payload,
                ):
                    continue
                if event.get("type") == "session.usage":
                    payload = event.get("payload")
                    if isinstance(payload, dict):
                        last_usage_payload = dict(payload)

                # 追踪 message.delta 累积文本
                if event.get("type") == "message.delta":
                    delta = event.get("payload", {}).get("delta", "")
                    if isinstance(delta, str) and delta:
                        _acc_delta += delta
                # 上游 OpenClaw runtime 可能将最后几个字符直接打包到 session.message
                # 的完整文本中，而未通过 assistant stream 以 delta 形式下发。此处检测
                # message.completed 文本是否比累积 delta 更长，补发缺失的末尾 delta。
                elif event.get("type") == "message.completed":
                    full_text = event.get("payload", {}).get("text", "")
                    if (
                        isinstance(full_text, str)
                        and len(full_text) > len(_acc_delta)
                        and full_text.startswith(_acc_delta)
                    ):
                        missing = full_text[len(_acc_delta):]
                        if missing:
                            yield {"type": "message.delta", "payload": {"delta": missing}}
                            _acc_delta = full_text

                yield event

    # 非流式发送时，仅拼接 message.delta 的 delta 作为最终结果。
    def send_message(self, session_id: str, message: str) -> RuntimeResult:
        text_parts: list[str] = []
        for event in self.run_turn(session_key=session_id, message=message):
            if event["type"] != "message.delta":
                continue
            delta = event["payload"].get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        return {"text": "".join(text_parts)}

    # 流式发送时，复用 run_turn 事件流并将 message.delta 转成 token_delta。
    def stream_message(
        self,
        session_id: str,
        message: str,
    ) -> Iterator[RuntimeChunk]:
        for event in self.run_turn(session_key=session_id, message=message):
            event_type = event.get("type")
            if event_type == "message.delta":
                delta = event["payload"].get("delta")
                if isinstance(delta, str) and delta:
                    yield {"type": "token_delta", "delta": delta}
            elif event_type in {"message.completed", "turn.completed"}:
                yield {"type": "done"}
                return

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
                yield {"type": "session.usage", "payload": normalized_payload}
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
                "type": "session.runtime.changed",
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
                    yield {"type": "message.delta", "payload": {"delta": delta}}
                return
            if stream == "thinking":
                delta = data.get("delta")
                text = data.get("text")
                if isinstance(delta, str) and delta:
                    yield {
                        "type": "thinking.delta",
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
                    yield {"type": "session.usage", "payload": usage_payload}
                return
            if stream == "lifecycle":
                phase = self._pick_string(data, "phase")
                if not isinstance(phase, str):
                    return
                normalized_phase = phase.lower()
                if normalized_phase == "start":
                    yield {"type": "message.started", "payload": {}}
                    return
                if normalized_phase == "end":
                    yield {"type": "turn.completed", "payload": {}}
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
                        "type": "stream.error",
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
                            "type": "tool.call.started",
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
                        yield {"type": "message.completed", "payload": {"text": text}}
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
            "type": "tool.call.response",
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
            yield {"type": "thinking", "payload": payload}

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
                "type": "tool.call.started",
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
                "type": "tool.call.response",
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
                "type": "tool.call.delta",
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

    def _should_skip_duplicate_started(
        self,
        *,
        event: RuntimeTurnEvent,
        seen_started_tool_calls: set[str],
    ) -> bool:
        if event.get("type") != "tool.call.started":
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

    def _should_skip_duplicate_usage(
        self,
        *,
        event: RuntimeTurnEvent,
        last_usage_payload: Mapping[str, Any] | None,
    ) -> bool:
        if event.get("type") != "session.usage":
            return False
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return False
        if last_usage_payload is not None and dict(last_usage_payload) == payload:
            return True
        return False

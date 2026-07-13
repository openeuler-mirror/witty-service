from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from typing import Any, cast

from witty_agent_server.application.models.agent import AgentStatus
from witty_agent_server.application.models.runtime_events import build_outbound_event
from witty_agent_server.application.services.agent import AgentService
from witty_agent_server.application.services.session_identity_store import (
    RuntimeSessionIdentity,
    SessionIdentityStore,
)
from witty_agent_server.application.services.session_state_sync_service import (
    SessionState,
    SessionStateSyncService,
)
from witty_agent_server.application.services.session import SessionService
from witty_agent_server.application.services.session import SessionServiceError
from witty_agent_server.runtimes.runtime_base import (
    RuntimeBase,
    RuntimeType,
    supports_runtime_lifecycle,
    supports_runtime_turn,
)
from witty_service.config import get_settings

logger = logging.getLogger(__name__)


class SessionWSOrchestratorError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class SessionWSOrchestrator:
    def __init__(
        self,
        *,
        session_service: SessionService,
        agent_service: AgentService,
        identity_store: SessionIdentityStore | None = None,
        state_sync_service: SessionStateSyncService | None = None,
        runtime_type: RuntimeType,
    ) -> None:
        self._session_service = session_service
        self._agent_service = agent_service
        self._identity_store = identity_store or SessionIdentityStore()
        self._state_sync_service = state_sync_service or SessionStateSyncService()
        self._runtime_type = runtime_type

    def stream_message(
        self, *, agent_id: str, session_id: str, message: str
    ) -> Iterator[Mapping[str, Any]]:
        self.precheck_message(agent_id=agent_id, session_id=session_id, message=message)

        session = self._require_session(agent_id=agent_id, session_id=session_id)
        runtime_type = cast(RuntimeType, session.get("runtime_type"))
        runtime = self._require_runtime(runtime_type)
        identity = self._resolve_identity(
            agent_id=agent_id,
            session_id=session_id,
            session=session,
            runtime_type=runtime_type,
        )

        self._append_session_event(
            agent_id=agent_id,
            session_id=session_id,
            type="message.created",
            source="user",
            payload={"message": message},
        )

        # 会话开始处理消息时，实时上报 running 状态。
        yield from self._emit_state_changed(
            agent_id=agent_id,
            session_id=session_id,
            runtime_type=runtime_type,
            state="running",
            reason="message.create",
        )

        turn_failed = False
        try:
            for event in self._run_runtime_turn(runtime, identity, message):
                if event.get("type") == "stream.error":
                    turn_failed = True
                yield from self._handle_runtime_event(
                    agent_id=agent_id,
                    session_id=session_id,
                    runtime_type=runtime_type,
                    identity=identity,
                    event=event,
                )
        except Exception as exc:
            code = getattr(exc, "code", "RUNTIME_UPSTREAM_ERROR")
            message_text = getattr(exc, "message", "runtime request failed")
            if not isinstance(code, str) or not code:
                code = "RUNTIME_UPSTREAM_ERROR"
            if not isinstance(message_text, str) or not message_text:
                message_text = "runtime request failed"
            payload = {
                "code": code,
                "message": message_text,
            }
            self._append_session_event(
                agent_id=agent_id,
                session_id=session_id,
                type="message.stream.failed",
                source="system",
                payload=payload,
            )
            turn_failed = True
            yield build_outbound_event(
                agent_id=agent_id,
                session_id=session_id,
                runtime_type=runtime_type,
                type="stream.error",
                payload=payload,
            )
        finally:
            # 本轮结束后实时上报状态：异常为 error，正常完成为 idle。
            target_state = "error" if turn_failed else "idle"
            yield from self._emit_state_changed(
                agent_id=agent_id,
                session_id=session_id,
                runtime_type=runtime_type,
                state=target_state,
                reason="turn.failed" if turn_failed else "turn.completed",
            )

    def abort_turn(self, *, agent_id: str, session_id: str) -> None:
         session = self._require_session(agent_id=agent_id, session_id=session_id)
         runtime_type = cast(RuntimeType, session.get("runtime_type"))
         runtime = self._require_runtime(runtime_type)
         if runtime is not None and supports_runtime_lifecycle(runtime):
             runtime_session_key = session.get("runtime_session_key")
             if isinstance(runtime_session_key, str) and runtime_session_key:
                 logger.info(
                     "abort runtime turn: agent_id=%s session_id=%s runtime_type=%s session_key=%s",
                     agent_id,
                     session_id,
                     runtime_type,
                     runtime_session_key,
                 )
                 try:
                     runtime.abort_session(session_key=runtime_session_key)
                 except Exception:
                     logger.exception(
                         "abort runtime turn failed: agent_id=%s session_id=%s",
                         agent_id,
                         session_id,
                     )
         # 中断会话后上报状态为 idle。
         self._state_sync_service.emit_state_changed(
             agent_id=agent_id,
             session_id=session_id,
             runtime_type=runtime_type,
             state="idle",
             reason="message.abort",
        )

    def precheck_message(self, *, agent_id: str, session_id: str, message: str) -> None:
        if not isinstance(message, str) or not message.strip():
            raise SessionWSOrchestratorError(
                code="INVALID_MESSAGE_PAYLOAD",
                message="invalid message payload",
                status_code=400,
            )

        self._require_running_agent()
        session = self._require_session(agent_id=agent_id, session_id=session_id)
        runtime_type = session.get("runtime_type")
        if not self._is_supported_runtime(runtime_type):
            raise SessionWSOrchestratorError(
                code="RUNTIME_UNAVAILABLE",
                message="runtime unavailable",
                status_code=503,
            )
        self._require_runtime(runtime_type)

    def _handle_runtime_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        runtime_type: RuntimeType,
        identity: RuntimeSessionIdentity,
        event: Mapping[str, Any],
    ) -> Iterator[Mapping[str, Any]]:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return

        payload = event.get("payload")
        normalized_payload = payload if isinstance(payload, dict) else {}

        if event_type == "session.runtime.changed":
            runtime_session_id = normalized_payload.get("runtime_session_id")
            if isinstance(runtime_session_id, str) and runtime_session_id:
                self._identity_store.refresh_runtime_session(
                    runtime_session_key=identity.runtime_session_key,
                    runtime_session_id=runtime_session_id,
                )
            forwarded_payload = dict(normalized_payload)
            forwarded_payload.setdefault(
                "runtime_session_key",
                identity.runtime_session_key,
            )
            yield build_outbound_event(
                agent_id=agent_id,
                session_id=session_id,
                runtime_type=runtime_type,
                type=event_type,
                payload=forwarded_payload,
            )
            return

        self._append_session_event(
            agent_id=agent_id,
            session_id=session_id,
            type=event_type,
            source=self._infer_event_source(event_type),
            payload=normalized_payload,
        )
        yield build_outbound_event(
            agent_id=agent_id,
            session_id=session_id,
            runtime_type=runtime_type,
            type=event_type,
            payload=normalized_payload,
        )

    def _append_session_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        type: str,
        source: str,
        payload: dict[str, Any],
    ) -> None:
        self._session_service.append_event(
            agent_id=agent_id,
            session_id=session_id,
            event={
                "type": type,
                "source": source,
                "payload": payload,
            },
        )

    def _emit_state_changed(
        self,
        *,
        agent_id: str,
        session_id: str,
        runtime_type: RuntimeType,
        state: SessionState,
        reason: str,
    ) -> Iterator[Mapping[str, Any]]:
        """发送并落库 session 状态变化事件。"""
        event = self._state_sync_service.build_state_changed_event(
            agent_id=agent_id,
            session_id=session_id,
            runtime_type=runtime_type,
            state=state,
            reason=reason,
        )
        if event is None:
            return

        payload = event.get("payload")
        normalized_payload = payload if isinstance(payload, dict) else {}
        self._append_session_event(
            agent_id=agent_id,
            session_id=session_id,
            type="session.state_changed",
            source="system",
            payload=normalized_payload,
        )
        logger.info(
            "emit session.state_changed: agent_id=%s session_id=%s state=%s reason=%s",
            agent_id,
            session_id,
            state,
            reason,
        )
        yield event

    def _resolve_identity(
        self,
        *,
        agent_id: str,
        session_id: str,
        session: Mapping[str, Any],
        runtime_type: RuntimeType,
    ) -> RuntimeSessionIdentity:
        runtime_session_key = session.get("runtime_session_key")
        if not isinstance(runtime_session_key, str) or not runtime_session_key:
            raise SessionWSOrchestratorError(
                code="INVALID_SESSION_RUNTIME_KEY",
                message="invalid session runtime key",
                status_code=500,
            )

        resolved = self._identity_store.resolve(agent_id=agent_id, session_id=session_id)
        if resolved is not None and resolved.runtime_session_key == runtime_session_key:
            return resolved

        return self._identity_store.bind(
            agent_id=agent_id,
            session_id=session_id,
            runtime_type=runtime_type,
            runtime_session_key=runtime_session_key,
            runtime_session_id=None,
        )

    def _run_runtime_turn(
        self,
        runtime: RuntimeBase,
        identity: RuntimeSessionIdentity,
        message: str,
    ) -> Iterator[Mapping[str, Any]]:
        if supports_runtime_turn(runtime):
            yield from runtime.run_turn(
                session_key=identity.runtime_session_key,
                message=message,
            )
            return

        text_parts: list[str] = []
        for chunk in runtime.stream_message(identity.runtime_session_key, message):
            chunk_type = chunk.get("type")
            if chunk_type in ("delta", "token_delta"):
                delta = chunk.get("delta")
                if isinstance(delta, str) and delta:
                    text_parts.append(delta)
                    yield {
                        "type": "message.delta",
                        "payload": {"delta": delta},
                    }
                continue

            if chunk_type == "done":
                yield {
                    "type": "message.completed",
                    "payload": {"text": "".join(text_parts)},
                }

    def _require_runtime(self, runtime_type: str) -> RuntimeBase:
        runtime = self._session_service.get_runtime(runtime_type)
        if runtime is None:
            raise SessionWSOrchestratorError(
                code="RUNTIME_UNAVAILABLE",
                message="runtime unavailable",
                status_code=503,
            )
        return runtime

    def _is_supported_runtime(self, runtime_type: object) -> bool:
        """判断 runtime_type 是否在 settings.runtime.supported 中。"""
        settings = get_settings()
        return isinstance(runtime_type, str) and runtime_type in settings.runtime.supported

    def _require_running_agent(self) -> None:
        if self._agent_service.agent.status != AgentStatus.RUNNING:
            raise SessionWSOrchestratorError(
                code="AGENT_NOT_RUNNING",
                message="agent is not running",
                status_code=409,
            )

    def _require_session(self, *, agent_id: str, session_id: str) -> dict[str, Any]:
        try:
            session = self._session_service.get_session(
                agent_id=agent_id,
                session_id=session_id,
            )
        except SessionServiceError as exc:
            raise SessionWSOrchestratorError(
                code=exc.code,
                message=exc.message,
                status_code=exc.status_code,
                details=exc.details,
            ) from exc
        if session is None:
            raise SessionWSOrchestratorError(
                code="SESSION_NOT_FOUND",
                message="session not found",
                status_code=404,
            )
        return session

    def _infer_event_source(self, event_type: str) -> str:
        if event_type.startswith("message"):
            return "assistant"
        if event_type.startswith("tool"):
            return "tool"
        if event_type.startswith("usage"):
            return "system"
        return "system"

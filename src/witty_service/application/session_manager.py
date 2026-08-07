from __future__ import annotations

from datetime import datetime
from typing import Protocol

import httpx

from witty_service.adapter.http_client import AdaptorHttpClient
from witty_service.domain.errors import (
    SESSION_NOT_FOUND as SESSION_NOT_FOUND,
)
from witty_service.domain.errors import (
    DomainError,
    session_not_found,
)
from witty_service.persistence.repositories import AgentRecord, SessionRecord

AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
SESSION_AGENT_MISMATCH = "SESSION_AGENT_MISMATCH"


class SessionRepository(Protocol):
    def create_session(self, agent_id: str) -> SessionRecord: ...

    def get_session(self, session_id: str) -> SessionRecord | None: ...

    def list_sessions(self, agent_id: str) -> list[SessionRecord]: ...

    def delete_session(self, session_id: str) -> None: ...

    def upsert_session(
        self,
        session_id: str,
        agent_id: str,
        status: str,
        context_initialized: bool = False,
        runtime_type: str | None = None,
        runtime_session_key: str | None = None,
        created_at: datetime | None = None,
        remote_runtime_agent_id: str | None = None,
    ) -> SessionRecord: ...

    def update_session_runtime_identity(
        self,
        *,
        session_id: str,
        runtime_type: str,
        runtime_session_id: str,
        runtime_session_key: str,
    ) -> SessionRecord: ...

    def get_agent(self, agent_id: str) -> AgentRecord | None: ...


class SessionManager:
    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository

    def create_session(self, agent_id: str) -> SessionRecord:
        if self._repository.get_agent(agent_id) is None:
            raise DomainError(
                code=AGENT_NOT_FOUND,
                message="Agent was not found.",
                details={"agent_id": agent_id},
            )
        return self._repository.create_session(agent_id)

    def get_session(self, agent_id: str, session_id: str) -> SessionRecord:
        self._require_agent(agent_id)
        session = self._repository.get_session(session_id)
        if session is None:
            raise session_not_found(session_id=session_id, agent_id=agent_id)
        if session.agent_id != agent_id:
            raise DomainError(
                code=SESSION_AGENT_MISMATCH,
                message="Session does not belong to the agent.",
                details={"agent_id": agent_id, "session_id": session_id},
            )
        return session

    def list_sessions(self, agent_id: str) -> list[SessionRecord]:
        self._require_agent(agent_id)
        return self._repository.list_sessions(agent_id)

    def delete_session(self, agent_id: str, session_id: str) -> None:
        self.get_session(agent_id, session_id)
        self._repository.delete_session(session_id)

    def upsert_session(
        self,
        session_id: str,
        agent_id: str,
        status: str,
        context_initialized: bool = False,
        runtime_type: str | None = None,
        runtime_session_key: str | None = None,
        created_at: datetime | None = None,
        remote_runtime_agent_id: str | None = None,
    ) -> SessionRecord:
        """直接在本地 repository 创建/更新 session"""
        return self._repository.upsert_session(
            session_id=session_id,
            agent_id=agent_id,
            status=status,
            context_initialized=context_initialized,
            runtime_type=runtime_type,
            runtime_session_key=runtime_session_key,
            created_at=created_at,
            remote_runtime_agent_id=remote_runtime_agent_id,
        )

    def update_session_runtime_identity(
        self,
        *,
        session_id: str,
        runtime_type: str,
        runtime_session_id: str,
        runtime_session_key: str,
    ) -> SessionRecord:
        return self._repository.update_session_runtime_identity(
            session_id=session_id,
            runtime_type=runtime_type,
            runtime_session_id=runtime_session_id,
            runtime_session_key=runtime_session_key,
        )

    async def resolve_runtime_agent_id(
        self,
        adaptor_client: AdaptorHttpClient,
        runtime_agent_id: str | None = None,
    ) -> str:
        """解析远端 runtime agent id，显式参数优先，默认 agent 兜底。"""
        if runtime_agent_id:
            return runtime_agent_id

        payload = await adaptor_client.list_agents()
        default_id = payload.get("defaultId")
        if isinstance(default_id, str) and default_id:
            return default_id

        agents = payload.get("agents")
        if isinstance(agents, list):
            for item in agents:
                if isinstance(item, dict) and item.get("default") is True:
                    item_id = item.get("id")
                    if isinstance(item_id, str) and item_id:
                        return item_id

        raise DomainError(
            code="RUNTIME_AGENT_DEFAULT_NOT_FOUND",
            message="Default runtime agent was not found.",
            details={},
        )

    async def create_session_remote(
        self,
        agent_id: str,
        adaptor_client: AdaptorHttpClient,
        runtime_agent_id: str | None = None,
    ) -> SessionRecord:
        """在 witty-agent-server 创建 session"""
        resolved_runtime_agent_id = await self.resolve_runtime_agent_id(
            adaptor_client=adaptor_client,
            runtime_agent_id=runtime_agent_id,
        )
        result = await adaptor_client.post(
            f"/agents/{resolved_runtime_agent_id}/sessions", json={}
        )
        session = self._repository.upsert_session(
            session_id=result["id"],
            agent_id=agent_id,
            status=result.get("status", "idle"),
            context_initialized=result.get("context_initialized", True),
            runtime_type=result.get("runtime_type"),
            created_at=datetime.fromisoformat(result["created_at"])
            if "created_at" in result
            else None,
            remote_runtime_agent_id=resolved_runtime_agent_id,
        )
        return session

    async def list_sessions_remote(
        self,
        agent_id: str,
        adaptor_client: AdaptorHttpClient,
        runtime_agent_id: str | None = None,
    ) -> list[SessionRecord]:
        """从 witty-agent-server 列出会话并刷新缓存"""
        resolved_runtime_agent_id = await self.resolve_runtime_agent_id(
            adaptor_client=adaptor_client,
            runtime_agent_id=runtime_agent_id,
        )
        result = await adaptor_client.get(
            f"/agents/{resolved_runtime_agent_id}/sessions"
        )
        sessions = []
        for item in result.get("sessions", []):
            session = self._repository.upsert_session(
                session_id=item["id"],
                agent_id=agent_id,
                status=item.get("status", "idle"),
                context_initialized=item.get("context_initialized", True),
                runtime_type=item.get("runtime_type"),
                created_at=datetime.fromisoformat(item["created_at"])
                if "created_at" in item
                else None,
                remote_runtime_agent_id=resolved_runtime_agent_id,
            )
            sessions.append(session)
        return sessions

    async def get_session_remote(
        self,
        agent_id: str,
        session_id: str,
        adaptor_client: AdaptorHttpClient,
        runtime_agent_id: str | None = None,
    ) -> SessionRecord:
        """从 witty-agent-server 获取 session 并刷新缓存"""
        local_session = self._repository.get_session(session_id)
        resolved_runtime_agent_id = (
            local_session.remote_runtime_agent_id if local_session is not None else None
        )
        if resolved_runtime_agent_id is None:
            resolved_runtime_agent_id = await self.resolve_runtime_agent_id(
                adaptor_client=adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        try:
            result = await adaptor_client.get(
                f"/agents/{resolved_runtime_agent_id}/sessions/{session_id}"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                self._repository.delete_session(session_id)
                raise session_not_found(
                    session_id=session_id, agent_id=agent_id
                ) from exc
            raise
        return self._repository.upsert_session(
            session_id=result["id"],
            agent_id=agent_id,
            status=result.get("status", "idle"),
            context_initialized=result.get("context_initialized", True),
            runtime_type=result.get("runtime_type"),
            created_at=datetime.fromisoformat(result["created_at"])
            if "created_at" in result
            else None,
            remote_runtime_agent_id=resolved_runtime_agent_id,
        )

    async def abort_session_remote(
        self,
        agent_id: str,
        session_id: str,
        adaptor_client: AdaptorHttpClient,
        runtime_agent_id: str | None = None,
    ) -> None:
        """透传到 witty-agent-server 中断 session"""
        local_session = self._repository.get_session(session_id)
        resolved_runtime_agent_id = (
            local_session.remote_runtime_agent_id if local_session is not None else None
        )
        if resolved_runtime_agent_id is None:
            resolved_runtime_agent_id = await self.resolve_runtime_agent_id(
                adaptor_client=adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        await adaptor_client.post(
            f"/agents/{resolved_runtime_agent_id}/sessions/{session_id}/abort",
            json={},
        )

    async def delete_session_remote(
        self,
        agent_id: str,
        session_id: str,
        adaptor_client: AdaptorHttpClient,
        runtime_agent_id: str | None = None,
    ) -> None:
        """透传到 witty-agent-server 删除 session"""
        local_session = self.get_session(agent_id, session_id)
        resolved_runtime_agent_id = local_session.remote_runtime_agent_id
        if resolved_runtime_agent_id is None:
            resolved_runtime_agent_id = await self.resolve_runtime_agent_id(
                adaptor_client=adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        await adaptor_client.post(
            f"/agents/{resolved_runtime_agent_id}/sessions/{session_id}/delete",
            json={},
        )
        self._repository.delete_session(session_id)

    def _require_agent(self, agent_id: str) -> None:
        if self._repository.get_agent(agent_id) is not None:
            return
        raise DomainError(
            code=AGENT_NOT_FOUND,
            message="Agent was not found.",
            details={"agent_id": agent_id},
        )

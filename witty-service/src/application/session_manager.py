from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from src.adapter.http_client import AdaptorHttpClient
from src.domain.errors import DomainError
from src.persistence.repositories import AgentRecord, SessionRecord

AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
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
        created_at: datetime | None = None,
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
            raise DomainError(
                code=SESSION_NOT_FOUND,
                message="Session was not found.",
                details={"agent_id": agent_id, "session_id": session_id},
            )
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

    async def create_session_remote(
        self,
        agent_id: str,
        adaptor_client: AdaptorHttpClient,
    ) -> SessionRecord:
        """在 witty-agent-server 创建 session"""
        result = await adaptor_client.post("/agent/sessions", json={})
        session = self._repository.upsert_session(
            session_id=result["id"],
            agent_id=agent_id,
            status="active",
            context_initialized=result.get("context_initialized", True),
            runtime_type=result.get("runtime_type"),
            created_at=datetime.fromisoformat(result["created_at"]) if "created_at" in result else None,
        )
        return session

    async def list_sessions_remote(
        self,
        agent_id: str,
        adaptor_client: AdaptorHttpClient,
    ) -> list[SessionRecord]:
        """从 witty-agent-server 列出会话并刷新缓存"""
        result = await adaptor_client.get("/agent/sessions")
        sessions = []
        for item in result.get("sessions", []):
            session = self._repository.upsert_session(
                session_id=item["id"],
                agent_id=agent_id,
                status=item.get("status", "active"),
                context_initialized=item.get("context_initialized", True),
                runtime_type=item.get("runtime_type"),
                created_at=datetime.fromisoformat(item["created_at"]) if "created_at" in item else None,
            )
            sessions.append(session)
        return sessions

    async def get_session_remote(
        self,
        agent_id: str,
        session_id: str,
        adaptor_client: AdaptorHttpClient,
    ) -> SessionRecord:
        """从 witty-agent-server 获取 session 并刷新缓存"""
        result = await adaptor_client.get(f"/agent/sessions/{session_id}")
        return self._repository.upsert_session(
            session_id=result["id"],
            agent_id=agent_id,
            status=result.get("status", "active"),
            context_initialized=result.get("context_initialized", True),
            runtime_type=result.get("runtime_type"),
            created_at=datetime.fromisoformat(result["created_at"]) if "created_at" in result else None,
        )

    async def delete_session_remote(
        self,
        session_id: str,
        adaptor_client: AdaptorHttpClient,
    ) -> None:
        """透传到 witty-agent-server 删除 session"""
        await adaptor_client.delete(f"/agent/sessions/{session_id}")
        self._repository.delete_session(session_id)

    def _require_agent(self, agent_id: str) -> None:
        if self._repository.get_agent(agent_id) is not None:
            return
        raise DomainError(
            code=AGENT_NOT_FOUND,
            message="Agent was not found.",
            details={"agent_id": agent_id},
        )

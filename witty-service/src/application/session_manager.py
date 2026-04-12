from __future__ import annotations

from typing import Protocol

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

    def _require_agent(self, agent_id: str) -> None:
        if self._repository.get_agent(agent_id) is not None:
            return
        raise DomainError(
            code=AGENT_NOT_FOUND,
            message="Agent was not found.",
            details={"agent_id": agent_id},
        )

from copy import deepcopy
from typing import Any

class InMemorySessionRepository:
    """基于内存的 session 仓储实现。"""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    def create(self, session: dict[str, Any]) -> dict[str, Any]:
        session_id = session["id"]
        self._sessions[session_id] = deepcopy(session)
        return deepcopy(self._sessions[session_id])

    def get(self, session_id: str) -> dict[str, Any] | None:
        session = self._sessions.get(session_id)
        return deepcopy(session) if session is not None else None

    def list(self) -> list[dict[str, Any]]:
        return [deepcopy(s) for s in self._sessions.values()]

    def delete(self, session_id: str) -> dict[str, Any] | None:
        session = self._sessions.pop(session_id, None)
        return deepcopy(session) if session is not None else None

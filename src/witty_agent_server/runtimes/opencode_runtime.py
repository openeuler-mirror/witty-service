from collections.abc import Iterator
from typing import Any

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.runtimes.runtime_base import (
    RuntimeBase,
    RuntimeChunk,
    RuntimeResult,
    RuntimeTurnEvent,
    RuntimeType,
)


class OpenCodeRuntime(RuntimeBase):
    runtime_type: RuntimeType = "opencode"

    def __init__(self, client: ClientBase | None = None) -> None:
        self._client = client

    def list_sessions(self, *, agent_id: str) -> list[dict[str, Any]]:
        if self._client is None:
            raise RuntimeError("opencode runtime requires a client")
        payload = self._client.list_sessions(agent_id=agent_id)
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        return sessions if isinstance(sessions, list) else []

    def create_session(self, *, session_key: str) -> None:
        if self._client is None:
            raise RuntimeError("opencode runtime requires a client")
        self._client.create_session(session_key=session_key)

    def delete_session(self, *, session_key: str) -> None:
        if self._client is None:
            raise RuntimeError("opencode runtime requires a client")
        self._client.delete_session(session_key=session_key)

    def abort_session(self, *, session_key: str) -> None:
        if self._client is None:
            raise RuntimeError("opencode runtime requires a client")
        self._client.abort_session(session_key=session_key)

    def run_turn(
        self,
        *,
        session_key: str,
        message: str,
    ) -> Iterator[RuntimeTurnEvent]:
        del session_key, message
        raise NotImplementedError("opencode runtime is not implemented yet")

    def send_message(self, session_id: str, message: str) -> RuntimeResult:
        raise NotImplementedError("opencode runtime is not implemented yet")

    def stream_message(self, session_id: str, message: str) -> Iterator[RuntimeChunk]:
        raise NotImplementedError("opencode runtime is not implemented yet")

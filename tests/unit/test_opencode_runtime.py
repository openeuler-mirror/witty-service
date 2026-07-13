from __future__ import annotations

from typing import Any

import pytest

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.runtimes.opencode_runtime import OpenCodeRuntime


class _RecordingClient(ClientBase):
    """记录所有调用，便于验证 OpenCodeRuntime 的转调。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._session_map: dict[str, str] = {}

    def list_agents(self) -> dict[str, Any]:
        self.calls.append(("list_agents", {}))
        return {"defaultId": "main", "agents": []}

    def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
        self.calls.append(("list_sessions", {"agent_id": agent_id}))
        return {"sessions": [{"id": "s1"}, {"id": "s2"}]}

    def get_agent(self, *, agent_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def create_session(self, *, session_key: str) -> None:
        self.calls.append(("create_session", {"session_key": session_key}))
        self._session_map[session_key] = f"opencode-id-{session_key}"

    def delete_session(self, *, session_key: str) -> None:
        self.calls.append(("delete_session", {"session_key": session_key}))
        self._session_map.pop(session_key, None)

    def abort_session(self, *, session_key: str) -> None:
        self.calls.append(("abort_session", {"session_key": session_key}))

    def stream_turn(self, *, session_key: str, message: str): 
        raise NotImplementedError


@pytest.mark.parametrize(
    "method, session_key",
    [
        ("create_session", "k1"),
        ("delete_session", "k2"),
        ("abort_session", "k3"),
    ],
)
def test_runtime_session_ops_delegate_to_client(method: str, session_key: str) -> None:
    client = _RecordingClient()
    runtime = OpenCodeRuntime(client=client)

    getattr(runtime, method)(session_key=session_key)

    assert client.calls == [(method, {"session_key": session_key})]


def test_runtime_list_sessions_delegates_and_unwraps_sessions_list() -> None:
    client = _RecordingClient()
    runtime = OpenCodeRuntime(client=client)

    result = runtime.list_sessions(agent_id="ignored")

    assert client.calls == [("list_sessions", {"agent_id": "ignored"})]
    assert result == [{"id": "s1"}, {"id": "s2"}]


def test_runtime_list_sessions_returns_empty_when_payload_missing() -> None:
    class _EmptyClient(_RecordingClient):
        def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
            return {}  # 没有 sessions key

    client = _EmptyClient()
    runtime = OpenCodeRuntime(client=client)

    assert runtime.list_sessions(agent_id="x") == []


def test_runtime_without_client_raises_on_session_ops() -> None:
    runtime = OpenCodeRuntime(client=None)

    with pytest.raises(RuntimeError):
        runtime.create_session(session_key="k")
    with pytest.raises(RuntimeError):
        runtime.delete_session(session_key="k")
    with pytest.raises(RuntimeError):
        runtime.abort_session(session_key="k")
    with pytest.raises(RuntimeError):
        runtime.list_sessions(agent_id="x")

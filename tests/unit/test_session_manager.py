from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from witty_service.application.session_manager import (
    AGENT_NOT_FOUND,
    SESSION_AGENT_MISMATCH,
    SESSION_NOT_FOUND,
    SessionManager,
)
from witty_service.domain.errors import DomainError


class RepositoryStub:
    def __init__(self) -> None:
        self.agents = {"agent-1": SimpleNamespace(id="agent-1")}
        self.sessions = {}
        self.deleted = []
        self.upserts = []
        self.identity_updates = []

    def create_session(self, agent_id: str):
        session = SimpleNamespace(id="session-new", agent_id=agent_id)
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str):
        return self.sessions.get(session_id)

    def list_sessions(self, agent_id: str):
        return [item for item in self.sessions.values() if item.agent_id == agent_id]

    def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)
        self.sessions.pop(session_id, None)

    def upsert_session(self, **kwargs):
        self.upserts.append(kwargs)
        session = SimpleNamespace(
            id=kwargs["session_id"],
            agent_id=kwargs["agent_id"],
            status=kwargs["status"],
            context_initialized=kwargs.get("context_initialized", False),
            runtime_type=kwargs.get("runtime_type"),
            runtime_session_id=kwargs.get("runtime_session_id"),
            runtime_session_key=kwargs.get("runtime_session_key"),
            created_at=kwargs.get("created_at"),
            remote_runtime_agent_id=kwargs.get("remote_runtime_agent_id"),
        )
        self.sessions[session.id] = session
        return session

    def update_session_runtime_identity(self, **kwargs):
        self.identity_updates.append(kwargs)
        session = self.sessions[kwargs["session_id"]]
        payload = dict(session.__dict__)
        payload.update(
            runtime_type=kwargs["runtime_type"],
            runtime_session_id=kwargs["runtime_session_id"],
            runtime_session_key=kwargs["runtime_session_key"],
        )
        updated = SimpleNamespace(**payload)
        self.sessions[updated.id] = updated
        return updated

    def get_agent(self, agent_id: str):
        return self.agents.get(agent_id)


class AdaptorClientStub:
    def __init__(self) -> None:
        self.posts = []
        self.gets = []
        self.agents_payload = {"defaultId": "runtime-default"}
        self.sessions_payload = {
            "sessions": [
                {
                    "id": "remote-session-1",
                    "status": "idle",
                    "context_initialized": True,
                    "runtime_type": "openclaw",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        }
        self.session_payload = {
            "id": "remote-session-1",
            "status": "running",
            "context_initialized": True,
            "runtime_type": "openclaw",
        }

    async def list_agents(self):
        return self.agents_payload

    async def post(self, path: str, json: dict):
        self.posts.append((path, json))
        if path.endswith("/sessions"):
            return {
                "id": "remote-session-1",
                "status": "idle",
                "context_initialized": True,
                "runtime_type": "openclaw",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        return {}

    async def get(self, path: str):
        self.gets.append(path)
        if path.endswith("/sessions"):
            return self.sessions_payload
        return self.session_payload


def test_create_get_list_and_delete_session() -> None:
    repo = RepositoryStub()
    manager = SessionManager(repo)

    created = manager.create_session("agent-1")
    fetched = manager.get_session("agent-1", created.id)
    listed = manager.list_sessions("agent-1")
    manager.delete_session("agent-1", created.id)

    assert fetched is created
    assert listed == [created]
    assert repo.deleted == [created.id]


def test_create_session_requires_existing_agent() -> None:
    manager = SessionManager(RepositoryStub())

    with pytest.raises(DomainError) as exc_info:
        manager.create_session("missing")

    assert exc_info.value.code == AGENT_NOT_FOUND


def test_get_session_raises_for_missing_session() -> None:
    manager = SessionManager(RepositoryStub())

    with pytest.raises(DomainError) as exc_info:
        manager.get_session("agent-1", "missing")

    assert exc_info.value.code == SESSION_NOT_FOUND
    assert exc_info.value.status_code == 404


def test_get_session_rejects_agent_mismatch() -> None:
    repo = RepositoryStub()
    repo.agents["agent-2"] = SimpleNamespace(id="agent-2")
    repo.sessions["session-1"] = SimpleNamespace(id="session-1", agent_id="agent-2")
    manager = SessionManager(repo)

    with pytest.raises(DomainError) as exc_info:
        manager.get_session("agent-1", "session-1")

    assert exc_info.value.code == SESSION_AGENT_MISMATCH


@pytest.mark.asyncio
async def test_resolve_runtime_agent_id_priority_and_fallbacks() -> None:
    manager = SessionManager(RepositoryStub())
    client = AdaptorClientStub()

    assert await manager.resolve_runtime_agent_id(client, "explicit") == "explicit"
    assert await manager.resolve_runtime_agent_id(client) == "runtime-default"

    client.agents_payload = {"agents": [{"id": "runtime-2", "default": True}]}
    assert await manager.resolve_runtime_agent_id(client) == "runtime-2"

    client.agents_payload = {"agents": []}
    with pytest.raises(DomainError) as exc_info:
        await manager.resolve_runtime_agent_id(client)
    assert exc_info.value.code == "RUNTIME_AGENT_DEFAULT_NOT_FOUND"


@pytest.mark.asyncio
async def test_remote_session_methods_sync_repository() -> None:
    repo = RepositoryStub()
    manager = SessionManager(repo)
    client = AdaptorClientStub()

    created = await manager.create_session_remote("agent-1", client)
    listed = await manager.list_sessions_remote("agent-1", client, "runtime-explicit")
    fetched = await manager.get_session_remote("agent-1", created.id, client)
    await manager.abort_session_remote("agent-1", created.id, client)
    await manager.delete_session_remote("agent-1", created.id, client)

    assert created.remote_runtime_agent_id == "runtime-default"
    assert listed[0].id == "remote-session-1"
    assert fetched.status == "running"
    assert client.posts == [
        ("/agents/runtime-default/sessions", {}),
        ("/agents/runtime-explicit/sessions/remote-session-1/abort", {}),
        ("/agents/runtime-explicit/sessions/remote-session-1/delete", {}),
    ]
    assert client.gets == [
        "/agents/runtime-explicit/sessions",
        "/agents/runtime-explicit/sessions/remote-session-1",
    ]
    assert repo.deleted == ["remote-session-1"]


@pytest.mark.asyncio
async def test_get_session_remote_404_cleans_local_session_cache() -> None:
    repo = RepositoryStub()
    manager = SessionManager(repo)
    client = AdaptorClientStub()
    repo.upsert_session(
        session_id="remote-session-404",
        agent_id="agent-1",
        status="idle",
        remote_runtime_agent_id="runtime-explicit",
    )

    async def raise_404(path: str):
        request = httpx.Request("GET", f"https://example.test{path}")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    client.get = raise_404

    with pytest.raises(DomainError) as exc_info:
        await manager.get_session_remote("agent-1", "remote-session-404", client)

    assert exc_info.value.code == SESSION_NOT_FOUND
    assert exc_info.value.status_code == 404
    assert repo.deleted == ["remote-session-404"]


def test_upsert_session_delegates_all_fields() -> None:
    repo = RepositoryStub()
    manager = SessionManager(repo)
    created_at = datetime.now(UTC)

    session = manager.upsert_session(
        session_id="session-1",
        agent_id="agent-1",
        status="idle",
        context_initialized=True,
        runtime_type="openclaw",
        created_at=created_at,
        remote_runtime_agent_id="runtime-1",
    )

    assert session.id == "session-1"
    assert repo.upserts[-1] == {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "status": "idle",
        "context_initialized": True,
        "runtime_type": "openclaw",
        "runtime_session_key": None,
        "created_at": created_at,
        "remote_runtime_agent_id": "runtime-1",
    }


def test_update_session_runtime_identity_delegates_to_repository() -> None:
    repo = RepositoryStub()
    manager = SessionManager(repo)
    repo.upsert_session(
        session_id="session-1",
        agent_id="agent-1",
        status="idle",
    )

    session = manager.update_session_runtime_identity(
        session_id="session-1",
        runtime_type="openclaw",
        runtime_session_id="runtime-session-1",
        runtime_session_key="agent:agent-1:session:session-1",
    )

    assert session.runtime_type == "openclaw"
    assert session.runtime_session_id == "runtime-session-1"
    assert session.runtime_session_key == "agent:agent-1:session:session-1"
    assert repo.identity_updates[-1] == {
        "session_id": "session-1",
        "runtime_type": "openclaw",
        "runtime_session_id": "runtime-session-1",
        "runtime_session_key": "agent:agent-1:session:session-1",
    }


@pytest.mark.asyncio
async def test_delete_session_remote_deletes_locally_when_runtime_delete_fails() -> (
    None
):
    """runtime 删除失败时仍删除本地会话，避免执行记录残留导致侧栏复活。"""
    repo = RepositoryStub()
    manager = SessionManager(repo)
    client = AdaptorClientStub()
    created = await manager.create_session_remote("agent-1", client)

    original_post = client.post

    async def failing_post(path: str, json: dict):
        if path.endswith("/delete"):
            raise httpx.ConnectError("runtime offline")
        return await original_post(path, json)

    client.post = failing_post  # type: ignore[method-assign]

    await manager.delete_session_remote("agent-1", created.id, client)

    assert created.id in repo.deleted
    assert repo.get_session(created.id) is None


@pytest.mark.asyncio
async def test_delete_session_remote_deletes_locally_when_runtime_resolution_fails() -> (
    None
):
    """runtime 不可达（解析默认 agent 失败）时仍删除本地会话。"""
    repo = RepositoryStub()
    manager = SessionManager(repo)
    client = AdaptorClientStub()
    created = await manager.create_session_remote("agent-1", client)
    # 清除 runtime 映射，强制走 resolve 路径并模拟失败。
    repo.upsert_session(
        session_id=created.id,
        agent_id="agent-1",
        status="idle",
        remote_runtime_agent_id=None,
    )
    client.agents_payload = {}

    await manager.delete_session_remote("agent-1", created.id, client)

    assert created.id in repo.deleted
    assert repo.get_session(created.id) is None

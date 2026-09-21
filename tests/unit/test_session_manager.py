from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from witty_service.application.session_manager import (
    SESSION_AGENT_MISMATCH,
    SessionManager,
)
from witty_service.domain.errors import AGENT_NOT_FOUND, SESSION_NOT_FOUND, DomainError


class RepositoryStub:
    def __init__(self) -> None:
        self.agents = {"agent-1": SimpleNamespace(id="agent-1")}
        self.sessions = {}
        self.deleted = []
        self.upserts = []
        self.identity_updates = []
        self.metadata_updates = []

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

    def update_session_metadata(self, session_id: str, *, title=None, pinned=None):
        self.metadata_updates.append(
            {"session_id": session_id, "title": title, "pinned": pinned}
        )
        session = self.sessions[session_id]
        payload = dict(session.__dict__)
        if title is not None:
            payload["title"] = title
        if pinned is not None:
            payload["pinned"] = pinned
        updated = SimpleNamespace(**payload)
        self.sessions[session_id] = updated
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


def test_update_session_metadata_applies_only_provided_fields() -> None:
    repo = RepositoryStub()
    repo.upsert_session(session_id="session-1", agent_id="agent-1", status="idle")
    manager = SessionManager(repo)

    updated = manager.update_session_metadata("agent-1", "session-1", title="新标题")

    assert updated.title == "新标题"
    assert repo.metadata_updates == [
        {"session_id": "session-1", "title": "新标题", "pinned": None}
    ]


def test_update_session_metadata_keeps_values_when_fields_omitted() -> None:
    """省略字段（含显式 None）不改任何值，与 PATCH /conversations 契约一致。"""
    repo = RepositoryStub()
    repo.upsert_session(session_id="session-1", agent_id="agent-1", status="idle")
    repo.sessions["session-1"].title = "原标题"
    manager = SessionManager(repo)

    updated = manager.update_session_metadata("agent-1", "session-1")

    assert updated.title == "原标题"
    assert repo.metadata_updates == [
        {"session_id": "session-1", "title": None, "pinned": None}
    ]


def test_update_session_metadata_rejects_session_of_another_agent() -> None:
    repo = RepositoryStub()
    repo.agents["agent-2"] = SimpleNamespace(id="agent-2")
    repo.sessions["session-1"] = SimpleNamespace(id="session-1", agent_id="agent-2")
    manager = SessionManager(repo)

    with pytest.raises(DomainError) as exc_info:
        manager.update_session_metadata("agent-1", "session-1", title="x")

    assert exc_info.value.code == SESSION_AGENT_MISMATCH
    assert repo.metadata_updates == []


def test_update_session_metadata_rejects_overlong_title() -> None:
    """长度校验收口在服务层：绕过 HTTP 层也不能写超长标题。"""
    repo = RepositoryStub()
    repo.upsert_session(session_id="session-1", agent_id="agent-1", status="idle")
    manager = SessionManager(repo)

    with pytest.raises(DomainError) as exc_info:
        manager.update_session_metadata("agent-1", "session-1", title="x" * 256)

    assert exc_info.value.code == "INVALID_SESSION_METADATA"
    assert exc_info.value.status_code == 400
    assert exc_info.value.details == {
        "field": "title",
        "reason": "too long",
        "max_length": 255,
    }
    assert repo.metadata_updates == []


def test_update_session_metadata_rejects_empty_title() -> None:
    repo = RepositoryStub()
    repo.upsert_session(session_id="session-1", agent_id="agent-1", status="idle")
    manager = SessionManager(repo)

    with pytest.raises(DomainError) as exc_info:
        manager.update_session_metadata("agent-1", "session-1", title="")

    assert exc_info.value.details["reason"] == "must not be empty"
    assert repo.metadata_updates == []


def test_get_session_does_not_reread_agent_row() -> None:
    """归属校验只读 session 一行（A）。

    sessions.agent_id 是 NOT NULL 外键（ondelete=CASCADE），会话行存在就意味着
    agent 行存在；原先顺带查的那次 agent 只是多一跳点查，而本仓库每个 repository
    调用都要新建 ORM Session（实测约 0.23ms/次），故只保留没有前置 agent 读取的
    入口里的 _require_agent。
    """
    repo = RepositoryStub()
    repo.sessions["session-1"] = SimpleNamespace(id="session-1", agent_id="agent-1")

    def _unexpected_agent_read(agent_id: str) -> object:
        raise AssertionError(f"get_session 不应再读 agent 行: {agent_id}")

    repo.get_agent = _unexpected_agent_read  # type: ignore[method-assign]

    fetched = SessionManager(repo).get_session("agent-1", "session-1")

    assert fetched.id == "session-1"


def test_list_sessions_still_requires_existing_agent() -> None:
    """没有前置 agent 读取的入口保留存在性校验：未知 agent 不得静默返回空列表。"""
    manager = SessionManager(RepositoryStub())

    with pytest.raises(DomainError) as exc_info:
        manager.list_sessions("missing")

    assert exc_info.value.code == AGENT_NOT_FOUND

"""会话恢复：`restore=True` 必须复用传入的 session_id，而不是另建一个运行时会话。

背景（2026-09-21 企微真机验收第 12 条）：服务重启后 `agent_manager` 会用

    POST /agents/{agent_id}/sessions {"session_id": <旧 id>, "restore": true}

把历史会话重填回 agent server 的内存缓存。opencode / dsh 走的是
`SessionServiceBase.create_session`，它此前**完全忽略** restore：每次都生成一个新
session_id 并新建一个运行时会话，HTTP 200 让上层记成"恢复成功"，但调用方拿着旧 id
提交时就报 SESSION_NOT_FOUND —— 表现为"重启后所有既存会话都发不出消息"。
"""

from __future__ import annotations

import pytest

from witty_agent_server.adapters.runtime_registry import RuntimeRegistry
from witty_agent_server.application.services.session.base import SessionServiceBase
from witty_agent_server.application.services.session.errors import (
    InvalidSessionConfigError,
    SessionNotFoundServiceError,
)
from witty_agent_server.application.services.session.openclaw_session_service import (
    OpenClawSessionService,
)
from witty_agent_server.infra.persistence.in_memory import InMemorySessionRepository

OLD_SESSION_ID = "2026-09-21-10-11-12:9f2abaa7-c446-4275-810e-961cbb8de76e"


class _FakeRuntime:
    """只记录生命周期调用；会话是否真的存在由测试自己断言。"""

    runtime_type = "opencode"

    def __init__(self) -> None:
        self.created: list[str] = []

    def create_session(self, *, session_key: str) -> None:
        self.created.append(session_key)

    def delete_session(self, *, session_key: str) -> None:
        return None

    def abort_session(self, *, session_key: str) -> None:
        return None


def _service() -> tuple[SessionServiceBase, _FakeRuntime]:
    runtime = _FakeRuntime()
    service = SessionServiceBase(
        runtime_registry=RuntimeRegistry(), repository=InMemorySessionRepository()
    )
    service.register_runtime(runtime)  # type: ignore[arg-type]
    return service, runtime


def test_create_session_without_restore_creates_a_runtime_session() -> None:
    """常规创建路径不受影响：仍然新建运行时会话。"""
    service, runtime = _service()

    session = service.create_session(agent_id="main", config={})

    assert session["id"] != OLD_SESSION_ID
    assert runtime.created == [session["runtime_session_key"]]


def test_restore_without_a_session_id_is_rejected() -> None:
    """只给 restore、不给 id 必须**当场拒绝**，而不是静默新建一行。

    旧行为：生成一个新 id 建行（并且因为 restore 为真而跳过运行时创建），HTTP 200
    让调用方以为恢复成功——它拿着自己那个 id 提交只会得到 SESSION_NOT_FOUND，
    库里还多出一条永远不会被用到的会话。两者都是"静默"的，所以最难查。
    """
    service, runtime = _service()

    with pytest.raises(InvalidSessionConfigError):
        service.create_session(agent_id="main", config={"restore": True})

    assert runtime.created == []
    assert service.list_sessions(agent_id="main") == []


@pytest.mark.parametrize("session_id", [None, "", "   ", 123])
def test_restore_with_a_blank_session_id_is_rejected(session_id: object) -> None:
    service, _ = _service()

    with pytest.raises(InvalidSessionConfigError):
        service.create_session(
            agent_id="main", config={"session_id": session_id, "restore": True}
        )


def test_a_session_id_without_restore_is_rejected() -> None:
    """不给 restore 就不能自选 session_id。

    仓储的 create 是**按 id upsert**：允许调用方自选主键，等于允许它覆盖别的会话
    （连 agent_id 一起改写）。要指定 id 就得明说是在恢复。
    """
    service, runtime = _service()

    with pytest.raises(InvalidSessionConfigError):
        service.create_session(agent_id="main", config={"session_id": OLD_SESSION_ID})

    assert service.list_sessions(agent_id="main") == []
    assert runtime.created == []


def test_restore_cannot_take_over_another_agents_session() -> None:
    """恢复只能落回自己的会话行：拿别人的 id 来恢复必须 404，且不能改写那条记录。"""
    service, _ = _service()
    owned = service.create_session(
        agent_id="other", config={"session_id": OLD_SESSION_ID, "restore": True}
    )
    assert owned["agent_id"] == "other"

    with pytest.raises(SessionNotFoundServiceError):
        service.create_session(
            agent_id="main", config={"session_id": OLD_SESSION_ID, "restore": True}
        )

    # 别人的那条会话必须原样还在（既没被删、也没被改写 agent_id）
    assert service.get_session(agent_id="other", session_id=OLD_SESSION_ID) is not None
    assert [item["id"] for item in service.list_sessions(agent_id="main")] == []


def test_restore_is_idempotent_for_the_same_agent() -> None:
    """agent_manager 恢复历史会话时可能重复调用：同一个 agent 再恢复一次必须成功。"""
    service, runtime = _service()

    first = service.create_session(
        agent_id="main", config={"session_id": OLD_SESSION_ID, "restore": True}
    )
    second = service.create_session(
        agent_id="main", config={"session_id": OLD_SESSION_ID, "restore": True}
    )

    assert first["id"] == second["id"] == OLD_SESSION_ID
    assert runtime.created == []


def test_openclaw_reuses_the_base_create_session() -> None:
    """OpenClaw 的那份 create_session 覆写已删除：两处逐字重复的代码会各自漂移。

    断言"还是同一个函数对象"而不是"行为一致"——重新抄一份实现就会让这条用例变红。
    """
    assert OpenClawSessionService.create_session is SessionServiceBase.create_session


def test_restore_reuses_the_session_id_and_skips_runtime_creation() -> None:
    """恢复路径：复用传入的 id，且**不**创建运行时会话。"""
    service, runtime = _service()

    session = service.create_session(
        agent_id="main", config={"session_id": OLD_SESSION_ID, "restore": True}
    )

    assert session["id"] == OLD_SESSION_ID
    assert session["runtime_session_key"] == f"agent:main:session:{OLD_SESSION_ID}"
    assert runtime.created == []
    # 关键断言：恢复后必须能按原 id 查到，否则 WS 侧仍然会是 SESSION_NOT_FOUND
    assert service.get_session(agent_id="main", session_id=OLD_SESSION_ID) is not None


def test_openclaw_service_restores_the_same_way() -> None:
    """删掉覆写之后，OpenClaw 走的就是基类实现：恢复路径必须完全一样。"""
    runtime = _FakeRuntime()
    service = OpenClawSessionService(
        runtime_registry=RuntimeRegistry(), repository=InMemorySessionRepository()
    )
    service.register_runtime(runtime)  # type: ignore[arg-type]

    session = service.create_session(
        agent_id="main", config={"session_id": OLD_SESSION_ID, "restore": True}
    )

    assert session["id"] == OLD_SESSION_ID
    assert runtime.created == []

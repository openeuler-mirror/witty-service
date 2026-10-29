"""会话恢复：`restore=True` 必须复用传入的 session_id，而不是另建一个运行时会话。

背景（2026-09-21 企微真机验收第 12 条）：服务重启后 `agent_manager` 会用

    POST /agents/{agent_id}/sessions {"session_id": <旧 id>, "restore": true}

把历史会话重填回 agent server 的内存缓存。opencode 走的是
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
    RuntimeSessionIdentityConflictError,
    RuntimeSessionNotFoundError,
    SessionNotFoundServiceError,
)
from witty_agent_server.application.services.session.opencode_session_service import (
    OpenCodeSessionService,
)
from witty_agent_server.infra.persistence.in_memory import InMemorySessionRepository

OLD_SESSION_ID = "2026-09-21-10-11-12:9f2abaa7-c446-4275-810e-961cbb8de76e"


class _AmbiguousLookupError(RuntimeError):
    """模拟 OpenCode 客户端在 title 命中多个会话时抛出的 409。"""

    status = 409


class _FakeRuntime:
    """只记录生命周期调用；会话是否真的存在由测试自己断言。"""

    runtime_type = "opencode"

    def __init__(self) -> None:
        self.created: list[str] = []
        self.bound: dict[str, str] = {}
        self.known: dict[str, str] = {}

    def create_session(self, *, session_key: str) -> str:
        self.created.append(session_key)
        runtime_session_id = f"oc-{len(self.created)}"
        self.known[session_key] = runtime_session_id
        return runtime_session_id

    def bind_session(self, *, session_key: str, runtime_session_id: str) -> None:
        self.bound[session_key] = runtime_session_id

    def find_session_id(self, *, session_key: str) -> str | None:
        return self.known.get(session_key)

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
    """常规创建路径不受影响：仍然新建运行时会话，并持久化它返回的原生 ID。"""
    service, runtime = _service()

    session = service.create_session(agent_id="main", config={})

    assert session["id"] != OLD_SESSION_ID
    assert runtime.created == [session["runtime_session_key"]]
    # 创建即落身份：Witty / Runtime 两侧 id 必须同时存在于同一条 session 记录里。
    assert session["runtime_session_id"] == "oc-1"


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
        agent_id="other",
        config={
            "session_id": OLD_SESSION_ID,
            "restore": True,
            "runtime_session_id": "oc-other",
        },
    )
    assert owned["agent_id"] == "other"

    with pytest.raises(SessionNotFoundServiceError):
        service.create_session(
            agent_id="main",
            config={
                "session_id": OLD_SESSION_ID,
                "restore": True,
                "runtime_session_id": "oc-other",
            },
        )

    # 别人的那条会话必须原样还在（既没被删、也没被改写 agent_id）
    assert service.get_session(agent_id="other", session_id=OLD_SESSION_ID) is not None
    assert [item["id"] for item in service.list_sessions(agent_id="main")] == []


def test_restore_is_idempotent_for_the_same_agent() -> None:
    """agent_manager 恢复历史会话时可能重复调用：同一个 agent 再恢复一次必须成功。"""
    service, runtime = _service()
    config = {
        "session_id": OLD_SESSION_ID,
        "restore": True,
        "runtime_session_id": "oc-legacy",
    }

    first = service.create_session(agent_id="main", config=config)
    second = service.create_session(agent_id="main", config=config)

    assert first["id"] == second["id"] == OLD_SESSION_ID
    assert runtime.created == []


def test_opencode_reuses_the_base_create_session() -> None:
    """OpenCode 直接继承基类 create_session：两处逐字重复的代码会各自漂移。

    断言"还是同一个函数对象"而不是"行为一致"——重新抄一份实现就会让这条用例变红。
    """
    assert OpenCodeSessionService.create_session is SessionServiceBase.create_session


def test_restore_reuses_the_session_id_and_skips_runtime_creation() -> None:
    """恢复路径：复用传入的 id，且**不**创建运行时会话。"""
    service, runtime = _service()

    session = service.create_session(
        agent_id="main",
        config={
            "session_id": OLD_SESSION_ID,
            "restore": True,
            "runtime_session_id": "oc-legacy",
        },
    )

    assert session["id"] == OLD_SESSION_ID
    assert session["runtime_session_key"] == f"agent:main:session:{OLD_SESSION_ID}"
    assert session["runtime_session_id"] == "oc-legacy"
    assert runtime.created == []
    # 恢复时必须把持久化 ID 绑定回 client cache，后续轮次不再走 title 回查。
    assert runtime.bound == {
        f"agent:main:session:{OLD_SESSION_ID}": "oc-legacy",
    }
    # 关键断言：恢复后必须能按原 id 查到，否则 WS 侧仍然会是 SESSION_NOT_FOUND
    assert service.get_session(agent_id="main", session_id=OLD_SESSION_ID) is not None


def test_restore_prefers_persisted_id_over_title_lookup() -> None:
    """有持久化 ID 时绝不能触碰 title 回查。"""
    service, runtime = _service()
    runtime.known[f"agent:main:session:{OLD_SESSION_ID}"] = "oc-from-title"
    lookups: list[str] = []
    runtime.find_session_id = lambda *, session_key: (  # type: ignore[method-assign]
        lookups.append(session_key) or "oc-from-title"
    )

    session = service.create_session(
        agent_id="main",
        config={
            "session_id": OLD_SESSION_ID,
            "restore": True,
            "runtime_session_id": "oc-persisted",
        },
    )

    assert session["runtime_session_id"] == "oc-persisted"
    assert lookups == []


def test_restore_backfills_legacy_identity_from_title_once() -> None:
    """旧数据没有 runtime_session_id：title 回查成功后由返回值回填。"""
    service, runtime = _service()
    key = f"agent:main:session:{OLD_SESSION_ID}"
    runtime.known[key] = "oc-legacy-title"

    first = service.create_session(
        agent_id="main",
        config={"session_id": OLD_SESSION_ID, "restore": True},
    )

    assert first["runtime_session_id"] == "oc-legacy-title"
    assert runtime.bound[key] == "oc-legacy-title"

    # 回填后再次恢复带上 ID，不应再查 title。
    lookups: list[str] = []
    runtime.find_session_id = lambda *, session_key: (  # type: ignore[method-assign]
        lookups.append(session_key) or "oc-legacy-title"
    )
    second = service.create_session(
        agent_id="main",
        config={
            "session_id": OLD_SESSION_ID,
            "restore": True,
            "runtime_session_id": first["runtime_session_id"],
        },
    )
    assert second["runtime_session_id"] == "oc-legacy-title"
    assert lookups == []


def test_restore_rejects_missing_runtime_session() -> None:
    """ID 与 title 都找不到 Runtime Session 时必须明确失败，不能静默恢复。"""
    service, _ = _service()

    with pytest.raises(RuntimeSessionNotFoundError) as exc_info:
        service.create_session(
            agent_id="main",
            config={"session_id": OLD_SESSION_ID, "restore": True},
        )

    assert exc_info.value.code == "RUNTIME_SESSION_NOT_FOUND"
    assert exc_info.value.status_code == 404


def test_restore_rejects_ambiguous_title_mapping() -> None:
    """同 title 多会话属于歧义映射，不能随机挑一个。"""
    service, runtime = _service()

    def ambiguous(*, session_key: str) -> str:
        raise _AmbiguousLookupError("title is ambiguous (2 sessions match)")

    runtime.find_session_id = ambiguous  # type: ignore[method-assign]

    with pytest.raises(RuntimeSessionIdentityConflictError) as exc_info:
        service.create_session(
            agent_id="main",
            config={"session_id": OLD_SESSION_ID, "restore": True},
        )

    assert exc_info.value.code == "RUNTIME_SESSION_IDENTITY_CONFLICT"
    assert exc_info.value.status_code == 409


def test_runtime_session_id_is_rejected_without_restore() -> None:
    """普通创建不接受调用方指定 Runtime 身份：它由 Runtime 返回。"""
    service, runtime = _service()

    with pytest.raises(InvalidSessionConfigError):
        service.create_session(
            agent_id="main", config={"runtime_session_id": "oc-forged"}
        )

    assert runtime.created == []


def test_opencode_service_restores_the_same_way() -> None:
    """OpenCode 走的就是基类实现：恢复路径必须完全一样。"""
    runtime = _FakeRuntime()
    service = OpenCodeSessionService(
        runtime_registry=RuntimeRegistry(), repository=InMemorySessionRepository()
    )
    service.register_runtime(runtime)  # type: ignore[arg-type]

    session = service.create_session(
        agent_id="main",
        config={
            "session_id": OLD_SESSION_ID,
            "restore": True,
            "runtime_session_id": "oc-legacy",
        },
    )

    assert session["id"] == OLD_SESSION_ID
    assert runtime.created == []

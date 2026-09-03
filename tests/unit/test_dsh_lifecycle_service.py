from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from witty_agent_server.application.models.agent import AgentStatus
from witty_agent_server.application.services.agent.dsh_agent_service import (
    DshAgentService,
)
from witty_agent_server.application.services.agent.dsh_lifecycle_service import (
    DshLifecycleError,
    DshLifecycleService,
)
from witty_agent_server.application.services.agent.errors import AgentServiceError
from witty_agent_server.infra.clients import dsh_client as dsh_client_module
from witty_agent_server.infra.clients.dsh_client import DshClient

# ---------------------------------------------------------------------------
# fake harness（配合真实 DshClient：monkeypatch DeepSeekHarness 构造函数）
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self) -> None:
        self.alive = True

    def poll(self) -> int | None:
        return None if self.alive else 0


class _FakeHarnessClient:
    def __init__(self) -> None:
        self._proc = _FakeProc()


class _FakeHarness:
    """覆盖 lifecycle 依赖的 SDK 表面：start / close / _initialized / client._proc。"""

    def __init__(self, config: object) -> None:
        self.config = config
        self.client = _FakeHarnessClient()
        self._initialized = False
        self.start_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self._initialized = True

    def close(self) -> None:
        self.close_calls += 1
        self._initialized = False
        self.client._proc = None


class _FailingHarness(_FakeHarness):
    def start(self) -> None:
        self.start_calls += 1
        raise OSError("spawn boom")


class _SurvivingCloseHarness(_FakeHarness):
    """close() 未能终止子进程（模拟 SDK 两级终止兜底失败）。"""

    def close(self) -> None:
        self.close_calls += 1  # 进程未死：_initialized / _proc 保持原样


def _patch_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mock_settings = MagicMock()
    mock_settings.workspace.root_path.return_value = tmp_path
    monkeypatch.setattr(
        "witty_agent_server.application.services.agent.dsh_lifecycle_service.get_settings",
        lambda: mock_settings,
    )


def _make_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    harness: type[_FakeHarness] | None = _FakeHarness,
    agent_id: str | None = "a1",
) -> tuple[DshLifecycleService, DshClient]:
    """构造已 patch 设置与可选 fake harness 的 client + lifecycle 服务。

    harness=None 不 patch 构造函数（纯配置用例）；agent_id=None 不
    update_config（未提供 agent_id 的 start 路径）。
    """
    _patch_settings(monkeypatch, tmp_path)
    if harness is not None:
        monkeypatch.setattr(dsh_client_module, "DeepSeekHarness", harness)
    client = DshClient()
    svc = DshLifecycleService(client=client)
    if agent_id is not None:
        svc.update_config(agent_id=agent_id)
    return svc, client


# ---------------------------------------------------------------------------
# DshLifecycleService
# ---------------------------------------------------------------------------


def test_update_config_derives_instance_paths_and_pushes_to_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path, agent_id=None)
    svc.update_config(agent_id="a1", model="deepseek-x", max_tokens=4096)

    assert client._workspace_dir == str(
        (tmp_path / "agent-workspaces" / "a1" / "workspace").resolve()
    )
    assert client._session_root == str(tmp_path / "dsh-instances" / "a1" / "sessions")
    assert client._model == "deepseek-x"
    assert client._max_tokens == 4096


@pytest.mark.parametrize(
    "bad_agent_id",
    ["../evil", "a/b", "/abs/path", "..", ".", "a\\b"],
)
def test_update_config_rejects_unsafe_agent_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_agent_id: str
) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path, harness=None, agent_id=None)
    with pytest.raises(DshLifecycleError) as exc:
        svc.update_config(agent_id=bad_agent_id)

    assert exc.value.action == "update_config"
    assert "invalid agent_id" in exc.value.message
    assert client._workspace_dir is None
    assert client._session_root is None


def test_update_config_change_detaches_running_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path)
    old_harness = client.ensure_harness()
    old_harness.start()
    assert svc.probe_running() is True

    svc.update_config(agent_id="a2")  # 真实变更 → detach 旧 harness

    assert old_harness.close_calls == 1
    assert client.harness is None
    assert svc.probe_running() is False

    svc.start_server()  # 下次 start 用新配置重建
    new_harness = client.harness
    assert new_harness is not None and new_harness is not old_harness
    assert client._workspace_dir == str(
        (tmp_path / "agent-workspaces" / "a2" / "workspace").resolve()
    )


def test_update_config_switch_agent_resets_model_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """切换 agent 时复位模型配置：上一 agent 的 api_key/model 不得沿用。"""
    svc, client = _make_lifecycle(monkeypatch, tmp_path, harness=None)
    svc.update_config(agent_id="a1", model="deepseek-x", api_key="sk-1")
    assert client._api_key == "sk-1"

    svc.update_config(agent_id="a2")

    assert client._api_key is None
    assert client._model == "deepseek-v4-flash"
    assert client._workspace_dir == str(
        (tmp_path / "agent-workspaces" / "a2" / "workspace").resolve()
    )


def test_update_config_with_unchanged_values_keeps_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path)
    svc.update_config(agent_id="a1", model="m")
    harness = client.ensure_harness()
    harness.start()

    svc.update_config(agent_id="a1", model="m")  # 幂等重放

    assert harness.close_calls == 0
    assert client.harness is harness
    assert svc.probe_running() is True


def test_start_server_creates_dirs_and_starts_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path)
    svc.start_server()

    assert (tmp_path / "agent-workspaces" / "a1" / "workspace").is_dir()
    assert (tmp_path / "dsh-instances" / "a1" / "sessions").is_dir()
    harness = client.harness
    assert harness is not None
    assert harness.start_calls == 1
    assert harness._initialized is True
    assert svc.probe_running() is True


def test_start_server_failure_closes_harness_and_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path, harness=_FailingHarness)
    with pytest.raises(DshLifecycleError) as exc:
        svc.start_server()

    assert exc.value.action == "start"
    assert "spawn boom" in exc.value.message
    assert client.harness is None
    assert svc.probe_running() is False


def test_start_server_dir_creation_failure_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 用一个文件占位使 mkdir 失败
    (tmp_path / "dsh-instances").write_text("not a dir", encoding="utf-8")
    svc, client = _make_lifecycle(monkeypatch, tmp_path)
    with pytest.raises(DshLifecycleError) as exc:
        svc.start_server()

    assert exc.value.action == "start"
    assert "failed to create dsh instance dirs" in exc.value.message
    assert client.harness is None


def test_start_server_without_agent_id_starts_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path, agent_id=None)
    svc.start_server()

    assert client.harness is not None
    assert client.harness.start_calls == 1
    assert not (tmp_path / "dsh-instances").exists()


def test_start_server_rebuilds_crashed_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path)
    svc.start_server()
    crashed = client.harness
    assert crashed is not None

    crashed.client._proc.alive = False  # 模拟子进程崩溃
    assert svc.probe_running() is False

    svc.start_server()  # 应重建而非复用

    rebuilt = client.harness
    assert rebuilt is not None and rebuilt is not crashed
    assert rebuilt.start_calls == 1
    assert svc.probe_running() is True


def test_probe_running_matrix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path)

    assert svc.probe_running() is False  # 无 harness

    harness = client.ensure_harness()  # 未 start（未完成握手）
    assert svc.probe_running() is False

    harness.start()  # 已握手且子进程存活
    assert svc.probe_running() is True

    harness.client._proc.alive = False  # 子进程已退出
    assert svc.probe_running() is False


def test_stop_closes_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path)
    svc.start_server()
    harness = client.harness
    assert harness is not None

    svc.stop()

    assert harness.close_calls == 1
    assert client.harness is None
    assert svc.probe_running() is False


def test_stop_without_harness_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, _ = _make_lifecycle(monkeypatch, tmp_path)
    svc.stop()  # 不应抛


def test_stop_raises_when_harness_survives_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    svc, client = _make_lifecycle(monkeypatch, tmp_path, harness=_SurvivingCloseHarness)
    svc.start_server()

    with pytest.raises(DshLifecycleError) as exc:
        svc.stop()

    assert exc.value.action == "stop"
    assert "still alive" in exc.value.message
    assert client.harness is None


def test_concurrent_start_server_creates_single_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[_FakeHarness] = []

    class _CountingHarness(_FakeHarness):
        def __init__(self, config: object) -> None:
            super().__init__(config)
            created.append(self)

    svc, client = _make_lifecycle(monkeypatch, tmp_path, harness=_CountingHarness)

    thread_count = 4
    barrier = threading.Barrier(thread_count)

    def worker() -> None:
        barrier.wait()
        svc.start_server()

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created) == 1
    assert client.harness is created[0]


# ---------------------------------------------------------------------------
# DshAgentService
# ---------------------------------------------------------------------------


class FakeDshLifecycle:
    """DshLifecycleControlPort 的可编程 fake。"""

    def __init__(self, *, probe_returns: list[bool] | None = None) -> None:
        self._probe_returns = list(probe_returns or [])
        self.start_calls = 0
        self.stop_calls = 0
        self.update_calls: list[dict[str, Any]] = []

    def probe_running(self) -> bool:
        if not self._probe_returns:
            return False
        return self._probe_returns.pop(0)

    def start_server(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def update_config(self, **kwargs: Any) -> None:
        self.update_calls.append(kwargs)


def test_agent_service_start_reuses_running_harness() -> None:
    svc = FakeDshLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)

    agent = service.start(agent_id="agent-1", reload=False)

    assert svc.start_calls == 0
    assert svc.stop_calls == 0
    assert service.last_start_already_running is True
    assert agent.status == AgentStatus.RUNNING
    assert agent.id == "agent-1"


def test_agent_service_start_starts_harness_when_not_running() -> None:
    svc = FakeDshLifecycle(probe_returns=[False])
    service = DshAgentService(lifecycle_service=svc)

    service.start(agent_id="agent-2")

    assert svc.start_calls == 1
    assert service.last_start_already_running is False
    assert service.agent.status == AgentStatus.RUNNING


def test_agent_service_start_with_reload_stops_then_starts() -> None:
    svc = FakeDshLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)

    service.start(agent_id="agent-3", reload=True)

    assert svc.stop_calls == 1
    assert svc.start_calls == 1


def test_agent_service_start_applies_dsh_config_to_lifecycle() -> None:
    svc = FakeDshLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)

    service.start(
        agent_id="agent-cfg",
        config={"dsh": {"model": "deepseek-x", "api_key": "sk-1", "max_tokens": 8192}},
        reload=False,
    )

    assert svc.update_calls == [
        {
            "agent_id": "agent-cfg",
            "model": "deepseek-x",
            "api_key": "sk-1",
            "max_tokens": 8192,
        }
    ]


def test_agent_service_start_sanitizes_api_key_from_config() -> None:
    """落库的 agent.config 剥离 dsh.api_key，但运行时下推仍携带。"""
    svc = FakeDshLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)

    service.start(
        agent_id="agent-sec",
        config={"dsh": {"model": "deepseek-x", "api_key": "sk-secret"}},
        reload=False,
    )

    assert service.agent.config == {"dsh": {"model": "deepseek-x"}}
    assert svc.update_calls == [
        {"agent_id": "agent-sec", "model": "deepseek-x", "api_key": "sk-secret"}
    ]


def test_agent_service_start_sanitizes_model_api_key_from_config() -> None:
    """落库的 agent.config 同时剥离 model.api_key（model 子对象也含凭据）。"""
    svc = FakeDshLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)

    service.start(
        agent_id="agent-sec-model",
        config={
            "dsh": {"model": "deepseek-x"},
            "model": {"name": "deepseek-x", "api_key": "sk-model-secret"},
        },
        reload=False,
    )

    assert service.agent.config == {
        "dsh": {"model": "deepseek-x"},
        "model": {"name": "deepseek-x"},
    }


def test_agent_service_start_without_config_still_pushes_agent_id() -> None:
    svc = FakeDshLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)

    service.start(reload=False)  # agent_id 缺省 → "main"

    assert svc.update_calls == [{"agent_id": "main"}]


def test_agent_service_start_invalid_agent_id_converts_to_400() -> None:
    class RejectingLifecycle(FakeDshLifecycle):
        def update_config(self, **kwargs: Any) -> None:
            raise DshLifecycleError(
                action="update_config", message="invalid agent_id '../evil'"
            )

    svc = RejectingLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)

    with pytest.raises(AgentServiceError) as exc:
        service.start(agent_id="../evil", reload=False)

    assert exc.value.code == "DSH_AGENT_CONFIG_INVALID"
    assert exc.value.status_code == 400
    assert exc.value.details["action"] == "update_config"
    assert svc.start_calls == 0  # 配置被拒，不再继续生命周期操作


@pytest.mark.parametrize(
    ("reload", "probe", "expect_stop", "message"),
    [
        (False, [False], 0, "harness boom"),
        (True, [True], 1, "post-stop boom"),
    ],
)
def test_agent_service_start_server_error_marks_failed(
    reload: bool, probe: list[bool], expect_stop: int, message: str
) -> None:
    """start_server 失败（冷启动 / reload 后重建）→ AgentServiceError(500) + FAILED。"""

    class FailingLifecycle(FakeDshLifecycle):
        def start_server(self) -> None:
            raise DshLifecycleError(action="start", message=message)

    svc = FailingLifecycle(probe_returns=probe)
    service = DshAgentService(lifecycle_service=svc)

    with pytest.raises(AgentServiceError) as exc:
        service.start(agent_id="a", reload=reload)

    assert exc.value.code == "DSH_RUNTIME_START_FAILED"
    assert exc.value.status_code == 500
    assert exc.value.details["action"] == "start"
    assert message in exc.value.details["message"]
    assert svc.stop_calls == expect_stop
    assert service.agent.status == AgentStatus.FAILED


def test_agent_service_stop_calls_lifecycle_stop() -> None:
    svc = FakeDshLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)
    service.start(agent_id="agent-4", reload=False)

    agent = service.stop()

    assert svc.stop_calls == 1
    assert agent.status == AgentStatus.STOPPED


def test_agent_service_stop_marks_failed_when_lifecycle_error() -> None:
    class BadLifecycle(FakeDshLifecycle):
        def stop(self) -> None:
            raise DshLifecycleError(action="stop", message="survived")

    svc = BadLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)
    service.start(agent_id="agent-x", reload=False)

    agent = service.stop()

    assert agent.status == AgentStatus.FAILED


def test_agent_service_stop_propagates_unexpected_error() -> None:
    class BadLifecycle(FakeDshLifecycle):
        def stop(self) -> None:
            raise RuntimeError("unexpected bug in lifecycle stop")

    svc = BadLifecycle(probe_returns=[True])
    service = DshAgentService(lifecycle_service=svc)
    service.start(agent_id="agent-unexpected", reload=False)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        service.stop()


def test_agent_service_list_agents_delegates_to_client() -> None:
    service = DshAgentService()

    result = service.list_agents()

    assert result["defaultId"] == "main"
    assert result["agents"][0]["id"] == "main"


def test_agent_service_resolve_default_agent() -> None:
    service = DshAgentService(lifecycle_service=FakeDshLifecycle())

    assert service.resolve_default_agent() == "main"


def test_agent_service_mcp_not_supported() -> None:
    service = DshAgentService(lifecycle_service=FakeDshLifecycle())

    with pytest.raises(NotImplementedError):
        service.setup_mcp(agent_id="a", mcp_server_name="s", mcp_server_config={"x": 1})
    with pytest.raises(NotImplementedError):
        service.unset_mcp(agent_id="a", mcp_server_name="s")

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import httpx
import pytest

from witty_agent_server.application.models.agent import AgentStatus
from witty_agent_server.application.services.agent._process_utils import (
    start_stderr_drainer,
)
from witty_agent_server.application.services.agent.opencode_agent_service import (
    OpenCodeAgentService,
)
from witty_agent_server.application.services.agent.opencode_lifecycle_service import (
    OpenCodeLifecycleError,
    OpenCodeLifecycleService,
    OpenCodeServeStartError,
    _build_opencode_model_config,
)
from witty_agent_server.infra.clients.opencode_client import OpenCodeClient

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _lifecycle_with_http_handler(
    handler,
    *,
    serve_port: int = 4096,
    username: str = "opencode",
    password: str = "",
    profile: str | None = None,
) -> OpenCodeLifecycleService:
    """构造 lifecycle，把长持有 httpx.Client 直接注入 client._http_client。

    `OpenCodeClient.http_client()` 现返回长持有实例，因此测试不再替换方法，
    而是把带 MockTransport 的 httpx.Client 塞进 `client._http_client`，让
    `probe_running`/`stop` 复用同一份 client。
    """
    client = OpenCodeClient(serve_port=serve_port, username=username, password=password)
    client._http_client = httpx.Client(
        base_url=client.server_url,
        auth=(username, password),
        transport=httpx.MockTransport(handler),
        timeout=3.0,
    )
    return OpenCodeLifecycleService(client=client, profile=profile)


# ---------------------------------------------------------------------------
# OpenCodeLifecycleService
# ---------------------------------------------------------------------------


class FakeLifecycleService(OpenCodeLifecycleService):
    """OpenCodeLifecycleService with HTTP/process parts faked."""

    def __init__(
        self,
        *,
        probe_returns: list[bool] | None = None,
        start_calls: int = 0,
        stop_calls: int = 0,
        client: OpenCodeClient | None = None,
    ) -> None:
        super().__init__(
            client=client or OpenCodeClient(),
        )
        self._probe_returns = list(probe_returns) if probe_returns is not None else []
        self.start_calls = start_calls
        self.stop_calls = stop_calls

    def probe_running(self) -> bool:
        if not self._probe_returns:
            return False
        return self._probe_returns.pop(0)

    def start_server(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


def test_probe_running_healthy_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/global/health"
        return httpx.Response(200, json={"healthy": True, "version": "1.2.3"})

    svc = _lifecycle_with_http_handler(handler, username="u", password="p")

    assert svc.probe_running() is True


def test_probe_running_unhealthy_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"healthy": False})

    svc = _lifecycle_with_http_handler(handler)

    assert svc.probe_running() is False


def test_probe_running_http_error_returns_false() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="err")

    svc = _lifecycle_with_http_handler(handler)

    assert svc.probe_running() is False


def test_probe_running_connection_error_returns_false() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no connection")

    svc = _lifecycle_with_http_handler(handler)

    assert svc.probe_running() is False


def test_probe_running_non_json_response_returns_false() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not json", headers={"Content-Type": "text/plain"}
        )

    svc = _lifecycle_with_http_handler(handler)

    assert svc.probe_running() is False


def test_stop_posts_dispose_then_terminates_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/instance/dispose"
        return httpx.Response(204)

    svc = _lifecycle_with_http_handler(handler)

    terminate_calls: list[bool] = []
    monkeypatch.setattr(
        svc, "_stop_serve_process", lambda: terminate_calls.append(True)
    )

    svc.stop()

    assert terminate_calls == [True]


def test_stop_falls_back_to_terminate_on_dispose_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    svc = _lifecycle_with_http_handler(handler)

    terminate_calls: list[bool] = []
    monkeypatch.setattr(
        svc, "_stop_serve_process", lambda: terminate_calls.append(True)
    )

    svc.stop()

    assert terminate_calls == [True]


def test_start_server_raises_when_executable_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = OpenCodeLifecycleService(client=OpenCodeClient())

    def fake_popen(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("opencode not found")

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(svc, "_stop_serve_process", lambda: None)

    with pytest.raises(OpenCodeServeStartError):
        svc.start_server()


class _StubProcess:
    """模拟 Popen，配合 stderr drainer 不阻塞。"""

    def __init__(self, *, stderr_text: str = "") -> None:
        self.returncode: int | None = None
        self._stderr = io.StringIO(stderr_text)

    @property
    def stderr(self) -> io.StringIO:
        return self._stderr

    def poll(self) -> int | None:
        return None  # never exits

    def communicate(self) -> tuple[str, str]:
        return "", ""

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


def test_start_server_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = OpenCodeLifecycleService(client=OpenCodeClient())

    stop_calls: list[bool] = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _StubProcess())
    monkeypatch.setattr(svc, "_stop_serve_process", lambda: stop_calls.append(True))
    monkeypatch.setattr(
        "witty_agent_server.application.services.agent.opencode_lifecycle_service.port_is_listening",
        lambda p: False,
    )

    call_count = {"n": 0}

    def fake_time() -> float:
        call_count["n"] += 1
        return float(call_count["n"] * 100.0)

    monkeypatch.setattr("time.time", fake_time)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    with pytest.raises(OpenCodeLifecycleError):
        svc.start_server()

    # _stop_serve_process 被调用两次：start_server() 顶部的 pre-cleanup +
    # 超时路径的 cleanup。
    assert stop_calls == [True, True]


# ---------------------------------------------------------------------------
# R3B：stderr drainer
# ---------------------------------------------------------------------------


def test_stderr_drainer_consumes_stderr_to_logger(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """stderr 行被 drainer 透传到 logger.warning。"""
    svc = OpenCodeLifecycleService(client=OpenCodeClient())

    proc = _StubProcess(stderr_text="err1\nerr2\n")

    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: proc)
    monkeypatch.setattr(
        "witty_agent_server.application.services.agent.opencode_lifecycle_service.port_is_listening",
        lambda p: False,
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)

    call_count = {"n": 0}
    monkeypatch.setattr(
        "time.time",
        lambda: (
            call_count.__setitem__("n", call_count["n"] + 1)
            or float(call_count["n"] * 100.0)
        ),
    )

    with (
        caplog.at_level(
            logging.WARNING,
            logger="witty_agent_server.application.services.agent.opencode_lifecycle_service",
        ),
        pytest.raises(OpenCodeLifecycleError),
    ):
        svc.start_server()

    # drainer 把两行 stderr 写 logger.warning
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("err1" in m for m in warnings)
    assert any("err2" in m for m in warnings)


def test_stop_serve_process_joins_drainer(monkeypatch: pytest.MonkeyPatch) -> None:
    """_stop_serve_process 应该 join drainer thread，确保不泄漏。"""
    svc = OpenCodeLifecycleService(client=OpenCodeClient())

    proc = _StubProcess(stderr_text="x\n")
    svc._serve_process = proc
    svc._stderr_drainer = start_stderr_drainer(
        proc,
        logger=logging.getLogger(
            "witty_agent_server.application.services.agent.opencode_lifecycle_service"
        ),
        log_prefix="opencode serve stderr",
        thread_name="opencode-stderr-drainer",
    )

    # 等 drainer 消费完 stderr（自然 EOF 退出）
    assert svc._stderr_drainer is not None
    svc._stderr_drainer.join(timeout=2)
    assert not svc._stderr_drainer.is_alive()

    # 重新赋一个 alive 状态的假 drainer，验证 _stop_serve_process 会 join
    class AliveThread:
        def __init__(self) -> None:
            self._joined = False
            self._alive = True

        def is_alive(self) -> bool:
            return self._alive

        def join(self, timeout: float | None = None) -> None:
            self._joined = True
            self._alive = False

    fake = AliveThread()
    svc._stderr_drainer = fake
    svc._serve_process = proc

    svc._stop_serve_process()

    assert fake._joined is True
    assert svc._stderr_drainer is None
    assert svc._serve_process is None


def test_stop_serve_process_kills_on_timeout_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TimeoutExpired 后应 kill 而非二段式判断 poll。"""
    svc = OpenCodeLifecycleService(client=OpenCodeClient())

    class _HangProcess(_StubProcess):
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(cmd=["x"], timeout=timeout or 0)

        kill_calls: ClassVar[list[bool]] = []

        def kill(self) -> None:
            self.kill_calls.append(True)
            self.returncode = -9
            # 之后再 wait 要正常返回，否则 kill wait 也会 TimeoutExpired 进入 except
            _HangProcess.wait = lambda self, timeout=None: -9

    import subprocess

    proc = _HangProcess()
    svc._serve_process = proc

    svc._stop_serve_process()

    assert proc.kill_calls == [True]


# ---------------------------------------------------------------------------
# OpenCodeAgentService
# ---------------------------------------------------------------------------


def test_stop_raises_when_process_survives_dispose_and_kill() -> None:
    """stop() 不变量校验：dispose + fallback kill 后进程仍存活时，
    显式 raise OpenCodeLifecycleError（让 agent_service.stop 的 except
    成为 live 而非 dead 分支）。model lifecycle.stop 内 never raise
    时本测试单一守护其真实统计意义。
    """
    svc = OpenCodeLifecycleService(client=OpenCodeClient())

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="dispose boom")

    svc._client._http_client = httpx.Client(
        base_url=svc._client.server_url,
        auth=(svc._client.username, svc._client.password),
        transport=httpx.MockTransport(handler),
        timeout=3.0,
    )

    proc = _StubProcess()
    # 模拟 terminate/kill 都失败：poll() 永远返回 None
    proc.poll = lambda: None
    proc.terminate = lambda: None
    proc.kill = lambda: None
    svc._serve_process = proc

    with pytest.raises(OpenCodeLifecycleError) as exc:
        svc.stop()

    assert exc.value.action == "stop"
    assert "still alive" in exc.value.message


def test_stop_does_not_raise_when_process_terminated_cleanly() -> None:
    """对照测试：dispose 失败但 fallback kill 成功（poll() 返回 rc）时，
    stop() 不应 raise——这是 except 分支不进、正常返回的 baseline 路径。
    """
    svc = OpenCodeLifecycleService(client=OpenCodeClient())

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="dispose boom")

    svc._client._http_client = httpx.Client(
        base_url=svc._client.server_url,
        auth=(svc._client.username, svc._client.password),
        transport=httpx.MockTransport(handler),
        timeout=3.0,
    )

    proc = _StubProcess()
    # _StubProcess.poll 硬编码 None；此处覆盖为模拟 terminate 后进程已退出
    proc.poll = lambda: 0
    svc._serve_process = proc

    svc.stop()  # 不应抛


def test_agent_service_start_reuses_running_serve() -> None:
    svc = FakeLifecycleService(probe_returns=[True])
    service = OpenCodeAgentService(lifecycle_service=svc)

    agent = service.start(agent_id="agent-1", reload=False)

    assert svc.start_calls == 0
    assert svc.stop_calls == 0
    assert service.last_start_already_running is True
    assert agent.status == AgentStatus.RUNNING
    assert agent.id == "agent-1"


def test_agent_service_start_starts_gateway_when_not_running() -> None:
    svc = FakeLifecycleService(probe_returns=[False])
    service = OpenCodeAgentService(lifecycle_service=svc)

    service.start(agent_id="agent-2")

    assert svc.start_calls == 1
    assert service.last_start_already_running is False


def test_agent_service_start_with_reload_stops_then_starts() -> None:
    svc = FakeLifecycleService(probe_returns=[True])
    svc.stop_calls = 0
    service = OpenCodeAgentService(lifecycle_service=svc)

    service.start(agent_id="agent-3", reload=True)

    assert svc.stop_calls == 1
    assert svc.start_calls == 1


def test_agent_service_stop_calls_lifecycle_stop() -> None:
    svc = FakeLifecycleService(probe_returns=[True])  # reuse path → RUNNING fast
    service = OpenCodeAgentService(lifecycle_service=svc)
    service.start(agent_id="agent-4", reload=False)

    agent = service.stop()

    assert svc.stop_calls == 1
    assert agent.status == AgentStatus.STOPPED


def test_agent_service_stop_marks_failed_when_lifecycle_error() -> None:
    """lifecycle.stop() 抛 OpenCodeLifecycleError 时，agent 状态应置为 FAILED
    而非 STOPPED——进程还活着，状态必须反映实际。"""

    class BadLifecycle(FakeLifecycleService):
        def __init__(self) -> None:
            super().__init__(
                probe_returns=[True]
            )  # probe True → start reuses, sets RUNNING

        def stop(self) -> None:
            raise OpenCodeLifecycleError(action="stop", message="boom")

    svc = BadLifecycle()
    service = OpenCodeAgentService(lifecycle_service=svc)

    service.start(agent_id="agent-x", reload=False)  # RUNNING via reuse path
    agent = service.stop()

    assert agent.status == AgentStatus.FAILED


def test_agent_service_stop_propagates_unexpected_error() -> None:
    """lifecycle.stop 抛非 OpenCodeLifecycleError 时，stop() 应向上抛而非静默吞。"""

    class BadLifecycle(FakeLifecycleService):
        def __init__(self) -> None:
            super().__init__(probe_returns=[True])

        def stop(self) -> None:
            raise RuntimeError("unexpected bug in lifecycle stop")

    svc = BadLifecycle()
    service = OpenCodeAgentService(lifecycle_service=svc)
    service.start(agent_id="agent-unexpected", reload=False)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        service.stop()


def test_agent_service_list_agents_delegates_to_client() -> None:
    """agent_service.list_agents 应直接委托 client 真打 /agent，不再硬编码 main。"""
    client = OpenCodeClient()
    client.list_agents = lambda: {
        "defaultId": "real-default",
        "agents": [{"id": "real-default", "default": True, "loaded": True}],
    }
    svc = FakeLifecycleService(client=client)
    service = OpenCodeAgentService(lifecycle_service=svc, client=client)

    result = service.list_agents()

    assert result["defaultId"] == "real-default"
    assert result["agents"][0]["id"] == "real-default"


def test_agent_service_resolve_default_agent() -> None:
    svc = FakeLifecycleService()
    service = OpenCodeAgentService(lifecycle_service=svc, agent=None)

    assert service.resolve_default_agent() == "main"


# ---------------------------------------------------------------------------
# update_config + _apply_config
# ---------------------------------------------------------------------------


def test_lifecycle_update_config_only_takes_profile() -> None:
    """lifecycle.update_config 接收 profile，连接参数归 client。"""
    client = OpenCodeClient(serve_port=4096)
    svc = OpenCodeLifecycleService(client=client)

    svc.update_config(profile="agent-001")

    # profile stored, connection params unaffected
    assert svc._profile == "agent-001"
    assert svc.serve_port == 4096
    assert svc.server_url == "http://127.0.0.1:4096"


def test_agent_service_start_applies_opencode_config_to_lifecycle_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_apply_config：连接字段喂 client、profile 喂 lifecycle。"""
    received: dict[str, object] = {}

    class RecordingLifecycle(FakeLifecycleService):
        def update_config(self, *, profile: str | None = None) -> None:
            received["profile"] = profile

    client = OpenCodeClient()

    def client_update_config(**kwargs: object) -> None:
        received["client_kwargs"] = kwargs

    client.update_config = client_update_config

    svc = RecordingLifecycle(probe_returns=[True], client=client)  # reuse path
    service = OpenCodeAgentService(lifecycle_service=svc, client=client)

    service.start(
        agent_id="agent-cfg",
        config={
            "opencode": {
                "serve_port": 7070,
                "profile": "agent-cfg-inst",
                "password": "secret",
                "username": "ocuser",
            }
        },
        reload=False,
    )

    # 连接字段下推给 client
    assert received["client_kwargs"] == {
        "serve_port": 7070,
        "password": "secret",
        "username": "ocuser",
    }
    # profile 下推给 lifecycle
    assert received["profile"] == "agent-cfg-inst"


def test_agent_service_start_without_opencode_config_does_not_call_update_config() -> (
    None
):
    lifecycle_calls: list[bool] = []
    client_calls: list[bool] = []

    class NoopLifecycle(FakeLifecycleService):
        def update_config(self, *, profile: str | None = None) -> None:
            lifecycle_calls.append(True)

    client = OpenCodeClient()

    def client_update_config(**kwargs: object) -> None:
        client_calls.append(True)

    client.update_config = client_update_config

    svc = NoopLifecycle(probe_returns=[True], client=client)
    service = OpenCodeAgentService(lifecycle_service=svc, client=client)

    service.start(agent_id="a", config={"model": {"provider": "x"}}, reload=False)

    assert lifecycle_calls == []
    assert client_calls == []


# ---------------------------------------------------------------------------
# agent service error conversion (symmetric with OpenClaw)
# ---------------------------------------------------------------------------


def test_agent_service_start_server_error_converts_to_agent_error() -> None:
    from witty_agent_server.application.services.agent.errors import AgentServiceError

    class FailingLifecycle(FakeLifecycleService):
        def start_server(self) -> None:
            raise OpenCodeServeStartError(message="serve start boom")

    svc = FailingLifecycle(probe_returns=[False])
    service = OpenCodeAgentService(lifecycle_service=svc)

    with pytest.raises(AgentServiceError) as exc:
        service.start(agent_id="a", reload=False)

    assert exc.value.code == "OPENCODE_SERVE_START_FAILED"
    assert "serve start boom" in exc.value.details["message"]
    # 失败后进程必定不存活，状态置 FAILED 避免与实际状态脱节
    assert service.agent.status == AgentStatus.FAILED


def test_agent_service_start_server_error_after_stop_marks_failed() -> None:
    """reload=True 路径下，旧进程已 stop 但新进程起不来时，state 也要回滚为 FAILED。"""

    class FailingLifecycle(FakeLifecycleService):
        def __init__(self) -> None:
            super().__init__(probe_returns=[True])
            self.stop_calls = 0

        def stop(self) -> None:
            self.stop_calls += 1

        def start_server(self) -> None:
            raise OpenCodeServeStartError(message="post-stop boom")

    svc = FailingLifecycle()
    service = OpenCodeAgentService(lifecycle_service=svc)

    from witty_agent_server.application.services.agent.errors import AgentServiceError

    with pytest.raises(AgentServiceError):
        service.start(agent_id="a", reload=True)

    assert svc.stop_calls == 1  # 旧进程已被 stop
    assert service.agent.status == AgentStatus.FAILED


# ---------------------------------------------------------------------------
# _setup_xdg_env
# ---------------------------------------------------------------------------


def test_setup_xdg_env_sets_all_xdg_variables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_setup_xdg_env 应设置全部 4 个 XDG 环境变量到正确路径。"""
    mock_settings = MagicMock()
    mock_settings.workspace.root_path.return_value = tmp_path
    monkeypatch.setattr(
        "witty_agent_server.application.services.agent.opencode_lifecycle_service.get_settings",
        lambda: mock_settings,
    )

    svc = OpenCodeLifecycleService(client=OpenCodeClient())
    env: dict[str, str] = {}

    svc._setup_xdg_env(env, "agent-001")

    inst_root = tmp_path / "opencode-instances" / "agent-001"
    assert env["XDG_DATA_HOME"] == str(inst_root / "data")
    assert env["XDG_STATE_HOME"] == str(inst_root / "state")
    assert env["XDG_CONFIG_HOME"] == str(
        (tmp_path / "agent-workspaces" / "agent-001" / "workspace").resolve()
    )
    assert env["XDG_CACHE_HOME"] == str(inst_root / "cache")


def test_setup_xdg_env_creates_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_setup_xdg_env 应创建 data/state/config/cache 目录。"""
    mock_settings = MagicMock()
    mock_settings.workspace.root_path.return_value = tmp_path
    monkeypatch.setattr(
        "witty_agent_server.application.services.agent.opencode_lifecycle_service.get_settings",
        lambda: mock_settings,
    )

    svc = OpenCodeLifecycleService(client=OpenCodeClient())
    env: dict[str, str] = {}

    svc._setup_xdg_env(env, "agent-002")

    inst_root = tmp_path / "opencode-instances" / "agent-002"
    assert (inst_root / "data").is_dir()
    assert (inst_root / "state").is_dir()
    assert (
        (tmp_path / "agent-workspaces" / "agent-002" / "workspace").resolve().is_dir()
    )
    assert (inst_root / "cache").is_dir()


# ---------------------------------------------------------------------------
# 回归防护：控制面声明的模型必须抵抗上游 models.dev 目录翻转
#
# 2026-09-28 事故:上游把 deepseek-v4-flash 标为 deprecated,opencode 启动时
# 把 config.provider.<id>.models 深合并进目录后 delete 该条目,使
# "deepseek/deepseek-v4-flash" 无法解析(ProviderModelNotFoundError,对外
# 表现为 HTTP 500 UnknownError),而 /global/health 依然 healthy,agent 状态
# 一直是 RUNNING -- 即"进程健康但每次调用都失败"。
# ---------------------------------------------------------------------------

_FLIP_MODEL = "deepseek-v4-flash"


def test_build_model_config_declares_status_active_for_custom_endpoint() -> None:
    cfg = _build_opencode_model_config(
        model_provider="deepseek",
        model_name=_FLIP_MODEL,
        api_key="sk-test",
        api_base_url="https://api.deepseek.com",
        compatibility="openai",
    )

    assert cfg is not None
    assert cfg["model"] == f"deepseek/{_FLIP_MODEL}"
    provider = cfg["provider"]["deepseek"]
    assert provider["models"][_FLIP_MODEL] == {
        "name": _FLIP_MODEL,
        "status": "active",
    }
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "https://api.deepseek.com"


def test_build_model_config_declares_status_active_for_builtin_provider() -> None:
    """无 api_base_url 的内置 provider 也必须声明 models 条目。

    这是 2026-09-24 那版补丁漏掉的分支:它把 models 写在 if api_base_url 里,
    内置 provider 完全不写,于是 100% 继承上游的 deprecated 状态。
    """
    cfg = _build_opencode_model_config(
        model_provider="anthropic",
        model_name="claude-sonnet-4",
        api_key="sk-test",
        api_base_url=None,
    )

    assert cfg is not None
    provider = cfg["provider"]["anthropic"]
    assert provider["models"]["claude-sonnet-4"] == {
        "name": "claude-sonnet-4",
        "status": "active",
    }
    # 内置 provider 不应被塞入 npm / baseURL(定义仍由 opencode 自带目录提供)
    assert "npm" not in provider
    assert "baseURL" not in provider.get("options", {})


def _disk_config_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[OpenCodeLifecycleService, Path]:
    """构造一个把 XDG 配置写到 tmp_path 的 lifecycle。"""
    cfg_path = tmp_path / "opencode.json"
    svc = OpenCodeLifecycleService(client=OpenCodeClient(), profile="agent-merge")
    monkeypatch.setattr(svc, "_opencode_config_path", lambda: cfg_path)
    return svc, cfg_path


def test_merge_model_into_disk_config_preserves_other_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整体替换 current["provider"] 会清掉文件里其它 provider 的声明。"""
    svc, cfg_path = _disk_config_lifecycle(tmp_path, monkeypatch)
    cfg_path.write_text(
        json.dumps(
            {
                "model": "kimi/kimi-k2.7-code",
                "provider": {
                    "kimi": {
                        "npm": "@ai-sdk/openai-compatible",
                        "models": {"kimi-k2.7-code": {"name": "kimi-k2.7-code"}},
                    }
                },
                "mcp": {"foo": {"type": "local", "enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    svc.configure_model(
        model_provider="deepseek",
        model_name=_FLIP_MODEL,
        api_key="sk-test",
        api_base_url="https://api.deepseek.com",
    )

    svc._merge_model_into_disk_config()

    written = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert written["model"] == f"deepseek/{_FLIP_MODEL}"
    assert set(written["provider"]) == {"kimi", "deepseek"}
    assert written["provider"]["kimi"]["models"]["kimi-k2.7-code"] == {
        "name": "kimi-k2.7-code"
    }
    assert written["mcp"] == {"foo": {"type": "local", "enabled": True}}


def test_merge_model_into_disk_config_preserves_sibling_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 provider 下其它已声明的模型也必须保留。"""
    svc, cfg_path = _disk_config_lifecycle(tmp_path, monkeypatch)
    cfg_path.write_text(
        json.dumps(
            {
                "model": "deepseek/deepseek-v4-pro",
                "provider": {
                    "deepseek": {
                        "models": {
                            "deepseek-v4-pro": {
                                "name": "deepseek-v4-pro",
                                "status": "active",
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    svc.configure_model(
        model_provider="deepseek",
        model_name=_FLIP_MODEL,
        api_key="sk-test",
        api_base_url="https://api.deepseek.com",
    )

    svc._merge_model_into_disk_config()

    written = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert set(written["provider"]["deepseek"]["models"]) == {
        "deepseek-v4-pro",
        _FLIP_MODEL,
    }
    assert written["provider"]["deepseek"]["models"][_FLIP_MODEL]["status"] == "active"


def test_merge_model_into_disk_config_preserves_sibling_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 provider 下 options 里的兄弟键不能被整体覆盖掉。"""
    svc, cfg_path = _disk_config_lifecycle(tmp_path, monkeypatch)
    cfg_path.write_text(
        json.dumps(
            {
                "model": "deepseek/deepseek-v4-pro",
                "provider": {
                    "deepseek": {
                        "options": {
                            "baseURL": "https://old.example.com",
                            "timeout": 30000,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    svc.configure_model(
        model_provider="deepseek",
        model_name=_FLIP_MODEL,
        api_key="sk-test",
        api_base_url="https://api.deepseek.com",
    )

    svc._merge_model_into_disk_config()

    written = json.loads(cfg_path.read_text(encoding="utf-8"))
    options = written["provider"]["deepseek"]["options"]
    # 新值覆盖同名键
    assert options["baseURL"] == "https://api.deepseek.com"
    assert options["apiKey"] == "sk-test"
    # 兄弟键必须保留
    assert options["timeout"] == 30000


# ---------------------------------------------------------------------------
# 启动后 preflight：确定的目录不可解析必须变成启动失败,而不是假 RUNNING;
# 探测接口本身不可用(无法得出结论)则告警放行。
# ---------------------------------------------------------------------------


def _catalog_handler(
    models: dict[str, Any] | None,
    *,
    provider_id: str = "deepseek",
    providers: list[Any] | None = None,
):
    """构造 /global/health + /provider 的 MockTransport handler。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        if request.url.path == "/provider":
            all_providers = (
                providers
                if providers is not None
                else [{"id": provider_id, "models": models or {}}]
            )
            return httpx.Response(200, json={"all": all_providers})
        return httpx.Response(404)

    return handler


def _startable_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    *,
    model_name: str | None = _FLIP_MODEL,
    profile: str = "agent-preflight",
) -> OpenCodeLifecycleService:
    """构造一个 start_server 能跑到 preflight 的 lifecycle(进程/端口全假)。"""
    mock_settings = MagicMock()
    mock_settings.workspace.root_path.return_value = tmp_path
    monkeypatch.setattr(
        "witty_agent_server.application.services.agent."
        "opencode_lifecycle_service.get_settings",
        lambda: mock_settings,
    )
    # instance_config_home 走 workspace_paths.agent_workspace_path,同样要隔离
    monkeypatch.setattr(
        "witty_service.workspace_paths.get_settings", lambda: mock_settings
    )
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _StubProcess())
    monkeypatch.setattr(
        "witty_agent_server.application.services.agent."
        "opencode_lifecycle_service.port_is_listening",
        lambda p: True,
    )

    svc = _lifecycle_with_http_handler(handler, profile=profile)
    if model_name is not None:
        svc.configure_model(
            model_provider="deepseek",
            model_name=model_name,
            api_key="sk-test",
            api_base_url="https://api.deepseek.com",
        )
    return svc


def test_start_server_raises_when_catalog_dropped_declared_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复刻事故现场:目录里只剩 deepseek-flash / deepseek-v4-pro。"""
    svc = _startable_lifecycle(
        tmp_path,
        monkeypatch,
        _catalog_handler(
            {
                "deepseek-flash": {"id": "deepseek-flash", "status": "active"},
                "deepseek-v4-pro": {"id": "deepseek-v4-pro", "status": "active"},
            }
        ),
    )
    stop_calls: list[bool] = []
    monkeypatch.setattr(svc, "_stop_serve_process", lambda: stop_calls.append(True))

    with pytest.raises(OpenCodeServeStartError) as exc:
        svc.start_server()

    assert f"deepseek/{_FLIP_MODEL}" in exc.value.message
    # 报错必须可执行:给出目录里现存的候选模型
    assert "deepseek-v4-pro" in exc.value.message
    # 顶部 pre-cleanup + 失败 cleanup:坏进程必须被停掉,不能留成假 RUNNING
    assert len(stop_calls) == 2


def test_start_server_raises_when_declared_model_deprecated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _startable_lifecycle(
        tmp_path,
        monkeypatch,
        _catalog_handler({_FLIP_MODEL: {"id": _FLIP_MODEL, "status": "deprecated"}}),
    )

    with pytest.raises(OpenCodeServeStartError) as exc:
        svc.start_server()

    assert "deprecated" in exc.value.message
    assert f"deepseek/{_FLIP_MODEL}" in exc.value.message


def test_start_server_raises_when_provider_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _startable_lifecycle(
        tmp_path, monkeypatch, _catalog_handler({}, providers=[])
    )

    with pytest.raises(OpenCodeServeStartError) as exc:
        svc.start_server()

    assert "deepseek" in exc.value.message


def test_start_server_retries_inconclusive_provider_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/provider 短暂 5xx 时应重试,不能把抖动当成模型缺失。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        if calls.count("/provider") < 2:
            return httpx.Response(500, text="catalog still building")
        return httpx.Response(
            200,
            json={
                "all": [
                    {"id": "deepseek", "models": {_FLIP_MODEL: {"status": "active"}}}
                ]
            },
        )

    svc = _startable_lifecycle(tmp_path, monkeypatch, handler)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    svc.start_server()  # 不应抛

    assert calls.count("/provider") == 2


def test_start_server_proceeds_when_preflight_inconclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """/provider 持续不可用只是"无法得出结论",不能否掉已 healthy 的 serve。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        return httpx.Response(500, text="boom")

    svc = _startable_lifecycle(tmp_path, monkeypatch, handler)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    with caplog.at_level(logging.WARNING):
        svc.start_server()  # 不应抛:不确定 ≠ 不可用

    assert any("inconclusive" in record.message for record in caplog.records)
    # 放行而非 cleanup:进程句柄仍在,说明没有被 _stop_serve_process 掐掉
    assert svc._serve_process is not None


def test_start_server_succeeds_when_declared_model_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _startable_lifecycle(
        tmp_path,
        monkeypatch,
        _catalog_handler(
            {
                _FLIP_MODEL: {"id": _FLIP_MODEL, "status": "active"},
                "deepseek-v4-pro": {"id": "deepseek-v4-pro", "status": "active"},
            }
        ),
    )

    svc.start_server()  # 不应抛:模型可解析时预检放行


def test_start_server_skips_preflight_without_model_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/global/health":
            return httpx.Response(200, json={"healthy": True})
        return httpx.Response(500, text="must not be reached")

    svc = _startable_lifecycle(tmp_path, monkeypatch, handler, model_name=None)

    svc.start_server()  # 不应抛

    assert "/provider" not in calls

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from witty_service.domain.errors import DomainError
from witty_service.sandbox.base import SandboxStatus
from witty_service.sandbox.factory import create_sandbox_backend


def _reset_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import witty_service.config as _config

    monkeypatch.setattr(_config, "_settings", None)


# =============================================================================
# Fake objects
# =============================================================================


@dataclass
class FakeContainer:
    id: str = "container-123"
    status: str = "running"
    attrs: dict[str, Any] = field(
        default_factory=lambda: {
            "NetworkSettings": {
                "Ports": {
                    "8080/tcp": [{"HostPort": "18080"}],
                },
            },
        }
    )

    def __post_init__(self) -> None:
        self.stop_called = False
        self.remove_called = False
        self.reload_called = False

    def stop(self, timeout: int = 10) -> None:
        self.stop_called = True
        self.status = "exited"

    def remove(self, force: bool = False) -> None:
        self.remove_called = True

    def reload(self) -> None:
        self.reload_called = True


class FakeContainers:
    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.run_calls: list[dict[str, object]] = []
        self._get_containers: dict[str, FakeContainer] = {}

    def run(self, image: str, **kwargs: object) -> FakeContainer:
        self.run_calls.append({"image": image, **kwargs})
        return self.container

    def get(self, container_name: str) -> FakeContainer:
        if container_name in self._get_containers:
            return self._get_containers[container_name]
        from docker.errors import NotFound

        raise NotFound(f"container {container_name} not found")


class FakeDockerClient:
    def __init__(self, container: FakeContainer) -> None:
        self.containers = FakeContainers(container)


class StopErrorContainer(FakeContainer):
    def stop(self, timeout: int = 10) -> None:
        self.stop_called = True
        raise RuntimeError("stop failed")


class RemoveErrorContainer(FakeContainer):
    def remove(self, force: bool = False) -> None:
        self.remove_called = True
        raise RuntimeError("remove failed")


class ReloadErrorContainer(FakeContainer):
    def reload(self) -> None:
        self.reload_called = True
        raise RuntimeError("reload failed")


class GetErrorContainers(FakeContainers):
    def get(self, container_name: str) -> FakeContainer:
        from docker.errors import APIError

        raise APIError("docker daemon error")


def _workspace_dir(tmp_path: Path) -> str:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return str(workspace.resolve())


# =============================================================================
# start() — basic flow
# =============================================================================


def test_docker_runtime_start_mounts_workspace_to_witty_workspace(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client, image="witty-agent")
    workspace_path = _workspace_dir(tmp_path)

    handle = backend.start(
        agent_id="agent-1",
        workspace_path=workspace_path,
        port=18080,
        image_tag="test",
    )

    assert client.containers.run_calls == [
        {
            "image": "witty-agent:test",
            "name": "witty-sandbox-agent-1",
            "detach": True,
            "user": "witty",
            "ports": {"8080/tcp": None},
            "volumes": {
                workspace_path: {
                    "bind": "/witty-workspace",
                    "mode": "rw",
                }
            },
            "environment": {},
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": "512m",
            "pids_limit": 100,
            "cpu_shares": 512,
            "read_only": True,
            "tmpfs": {
                "/tmp": "rw,noexec,nosuid,size=256M",
                "/home/witty": "rw,nosuid,uid=1000,gid=1000,mode=0755,size=256M",
            },
            "ulimits": [
                {"name": "nofile", "soft": 1024, "hard": 4096},
            ],
        }
    ]
    assert handle.workspace_path == workspace_path
    assert handle.metadata["container_id"] == "container-123"
    assert handle.metadata["host_port"] == 18080
    assert handle.metadata["base_url"] == "http://127.0.0.1:18080"
    assert handle.metadata["container_workspace_path"] == "/witty-workspace"


def test_docker_runtime_start_with_environment_vars(tmp_path: Path) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client, image="witty-agent")
    workspace_path = _workspace_dir(tmp_path)

    backend.start(
        agent_id="agent-env",
        workspace_path=workspace_path,
        port=18090,
        image_tag="test",
        env={"API_KEY": "secret", "DEBUG": "true"},
    )

    assert client.containers.run_calls[0]["environment"] == {
        "API_KEY": "secret",
        "DEBUG": "true",
    }


# =============================================================================
# stop / cleanup / endpoint
# =============================================================================


def test_docker_runtime_stop_cleanup_and_endpoint(tmp_path: Path) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    workspace_path = _workspace_dir(tmp_path)
    handle = backend.start(
        agent_id="agent-2",
        workspace_path=workspace_path,
        image_tag="test",
    )

    endpoint = backend.endpoint(handle)
    backend.stop(handle)
    backend.cleanup(handle)

    assert endpoint.base_url == "http://127.0.0.1:18080"
    assert endpoint.health_url == "http://127.0.0.1:18080/ping"
    assert container.stop_called is True
    assert container.remove_called is True
    assert handle.sandbox_id not in backend._handles
    assert handle.sandbox_id not in backend._containers


# =============================================================================
# status() — container state mapping
# =============================================================================


@pytest.mark.parametrize(
    ("container_status", "expected_status"),
    [
        ("created", SandboxStatus.starting),
        ("restarting", SandboxStatus.starting),
        ("running", SandboxStatus.running),
        ("exited", SandboxStatus.stopped),
        ("paused", SandboxStatus.stopped),
        ("removing", SandboxStatus.stopped),
        ("dead", SandboxStatus.error),
        ("mystery", SandboxStatus.error),
    ],
)
def test_docker_runtime_status_maps_container_state(
    tmp_path: Path,
    container_status: str,
    expected_status: SandboxStatus,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer(status=container_status)
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    workspace_path = _workspace_dir(tmp_path)
    handle = backend.start(
        agent_id="agent-3",
        workspace_path=workspace_path,
        port=18082,
        image_tag="test",
    )

    assert backend.status(handle) is expected_status
    assert container.reload_called is True


def test_docker_runtime_status_returns_stopped_when_no_container(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    workspace_path = _workspace_dir(tmp_path)
    handle = backend.start(
        agent_id="agent-no-container",
        workspace_path=workspace_path,
        port=18100,
        image_tag="test",
    )
    backend._containers.pop(handle.sandbox_id, None)

    assert backend.status(handle) is SandboxStatus.stopped


def test_docker_runtime_status_raises_domain_error_when_docker_api_fails(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = ReloadErrorContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    handle = backend.start(
        agent_id="agent-4",
        workspace_path=_workspace_dir(tmp_path),
        port=18083,
        image_tag="test",
    )

    with pytest.raises(DomainError) as exc_info:
        backend.status(handle)

    assert exc_info.value.code == "SANDBOX_STOP_FAILED"
    assert exc_info.value.details["sandbox_id"] == handle.sandbox_id
    assert exc_info.value.details["container_id"] == "container-123"


# =============================================================================
# cleanup() — error handling
# =============================================================================


def test_docker_runtime_cleanup_attempts_remove_when_stop_fails(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = StopErrorContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    handle = backend.start(
        agent_id="agent-5",
        workspace_path=_workspace_dir(tmp_path),
        port=18084,
        image_tag="test",
    )

    with pytest.raises(DomainError) as exc_info:
        backend.cleanup(handle)

    assert exc_info.value.code == "SANDBOX_STOP_FAILED"
    assert container.stop_called is True
    assert container.remove_called is True
    assert handle.sandbox_id not in backend._handles
    assert handle.sandbox_id not in backend._containers


def test_docker_runtime_cleanup_when_remove_fails(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = RemoveErrorContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    handle = backend.start(
        agent_id="agent-6",
        workspace_path=_workspace_dir(tmp_path),
        port=18085,
        image_tag="test",
    )

    with pytest.raises(DomainError) as exc_info:
        backend.cleanup(handle)

    assert exc_info.value.code == "SANDBOX_STOP_FAILED"
    assert container.stop_called is True
    assert container.remove_called is True
    assert handle.sandbox_id not in backend._handles
    assert handle.sandbox_id not in backend._containers


def test_docker_runtime_cleanup_silent_when_no_container(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    workspace_path = _workspace_dir(tmp_path)
    handle = backend.start(
        agent_id="agent-cleanup-none",
        workspace_path=workspace_path,
        port=18101,
        image_tag="test",
    )
    backend._containers.pop(handle.sandbox_id, None)

    backend.cleanup(handle)

    assert handle.sandbox_id not in backend._handles


# =============================================================================
# stop() — error handling
# =============================================================================


def test_docker_runtime_stop_raises_domain_error_when_docker_api_fails(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = StopErrorContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    handle = backend.start(
        agent_id="agent-stop",
        workspace_path=_workspace_dir(tmp_path),
        port=18086,
        image_tag="test",
    )

    with pytest.raises(DomainError) as exc_info:
        backend.stop(handle)

    assert exc_info.value.code == "SANDBOX_STOP_FAILED"
    assert exc_info.value.details["sandbox_id"] == handle.sandbox_id
    assert exc_info.value.details["container_id"] == "container-123"
    assert container.stop_called is True


def test_docker_runtime_stop_skips_when_no_container(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    workspace_path = _workspace_dir(tmp_path)
    handle = backend.start(
        agent_id="agent-stop-none",
        workspace_path=workspace_path,
        port=18102,
        image_tag="test",
    )
    backend._containers.pop(handle.sandbox_id, None)

    backend.stop(handle)


# =============================================================================
# start() — workspace path validation
# =============================================================================


def test_docker_runtime_start_rejects_invalid_workspace_path() -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    backend = DockerSandboxBackend(client=FakeDockerClient(FakeContainer()))

    with pytest.raises(DomainError) as exc_info:
        backend.start(agent_id="agent-7", workspace_path="relative/workspace")

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert exc_info.value.details["path"] == "relative/workspace"


def test_docker_runtime_start_rejects_missing_workspace_path(tmp_path: Path) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    backend = DockerSandboxBackend(client=FakeDockerClient(FakeContainer()))
    missing_path = str((tmp_path / "missing").resolve())

    with pytest.raises(DomainError) as exc_info:
        backend.start(agent_id="agent-8", workspace_path=missing_path)

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert exc_info.value.details["path"] == missing_path


def test_docker_runtime_start_rejects_file_as_workspace_path(tmp_path: Path) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    backend = DockerSandboxBackend(client=FakeDockerClient(FakeContainer()))
    file_path = tmp_path / "a_file.txt"
    file_path.write_text("content")

    with pytest.raises(DomainError) as exc_info:
        backend.start(agent_id="agent-9", workspace_path=str(file_path.resolve()))

    assert exc_info.value.code == "SANDBOX_START_FAILED"


# =============================================================================
# start() — agent_id deduplication & container reconnection
# =============================================================================


def test_docker_runtime_start_returns_existing_handle_for_same_agent_id(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    workspace_path = _workspace_dir(tmp_path)

    handle1 = backend.start(
        agent_id="agent-dup",
        workspace_path=workspace_path,
        port=18110,
        image_tag="test",
    )
    handle2 = backend.start(
        agent_id="agent-dup",
        workspace_path=workspace_path,
        port=18111,
    )

    assert handle1.sandbox_id == handle2.sandbox_id
    assert len(backend._handles) == 1


def test_docker_runtime_start_reconnects_existing_running_container(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer(
        id="existing-container-456",
        status="running",
        attrs={
            "NetworkSettings": {
                "Ports": {
                    "8080/tcp": [{"HostPort": "19000"}],
                },
            },
        },
    )
    client = FakeDockerClient(container)
    client.containers._get_containers["witty-sandbox-agent-reconnect"] = container

    backend = DockerSandboxBackend(client=client)
    workspace_path = _workspace_dir(tmp_path)

    handle = backend.start(
        agent_id="agent-reconnect",
        workspace_path=workspace_path,
        port=18112,
    )

    assert handle.metadata["reconnected"] is True
    assert handle.metadata["container_id"] == "existing-container-456"
    assert handle.metadata["host_port"] == 19000
    assert handle.metadata["base_url"] == "http://127.0.0.1:19000"


def test_docker_runtime_start_ignores_non_running_existing_container(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    stopped_container = FakeContainer(
        id="stopped-container",
        status="exited",
    )
    client = FakeDockerClient(stopped_container)
    client.containers._get_containers["witty-sandbox-agent-stopped"] = stopped_container

    backend = DockerSandboxBackend(client=client, image="witty-agent")
    workspace_path = _workspace_dir(tmp_path)

    handle = backend.start(
        agent_id="agent-stopped",
        workspace_path=workspace_path,
        port=18113,
        image_tag="test",
    )

    assert "reconnected" not in handle.metadata
    assert client.containers.run_calls[0]["name"] == "witty-sandbox-agent-stopped"


def test_docker_runtime_start_propagates_container_get_error(
    tmp_path: Path,
) -> None:
    from docker.errors import APIError

    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    client.containers = GetErrorContainers(container)

    backend = DockerSandboxBackend(client=client, image="witty-agent")
    workspace_path = _workspace_dir(tmp_path)

    with pytest.raises(APIError, match="docker daemon error"):
        backend.start(
            agent_id="agent-get-error",
            workspace_path=workspace_path,
        )


# =============================================================================
# _extract_host_port — error cases
# =============================================================================


@pytest.mark.parametrize(
    ("bad_attrs", "description"),
    [
        ({"NetworkSettings": {"Ports": {}}}, "missing port key"),
        ({"NetworkSettings": {"Ports": {"8080/tcp": []}}}, "empty port bindings"),
        ({"NetworkSettings": {"Ports": None}}, "null ports"),
    ],
)
def test_docker_runtime_extract_host_port_errors(
    bad_attrs: dict[str, Any],
    description: str,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer(attrs=bad_attrs)
    backend = DockerSandboxBackend(client=FakeDockerClient(container))

    with pytest.raises(DomainError) as exc_info:
        backend._extract_host_port(container)

    assert exc_info.value.code == "SANDBOX_START_FAILED"


# =============================================================================
# _try_find_container
# =============================================================================


def test_docker_runtime_try_find_container_returns_running() -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer(status="running")
    client = FakeDockerClient(container)
    client.containers._get_containers["witty-sandbox-agent-1"] = container

    backend = DockerSandboxBackend(client=client)

    result = backend._try_find_container("witty-sandbox-agent-1")

    assert result is container
    assert container.reload_called is True


def test_docker_runtime_try_find_container_removes_non_running() -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer(status="exited")
    client = FakeDockerClient(container)
    client.containers._get_containers["witty-sandbox-agent-2"] = container

    backend = DockerSandboxBackend(client=client)

    result = backend._try_find_container("witty-sandbox-agent-2")

    assert result is None
    assert container.remove_called is True


def test_docker_runtime_try_find_container_raises_on_api_error() -> None:
    from docker.errors import APIError

    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    client.containers = GetErrorContainers(container)

    backend = DockerSandboxBackend(client=client)

    with pytest.raises(APIError, match="docker daemon error"):
        backend._try_find_container("witty-sandbox-nonexistent")


# =============================================================================
# endpoint() — ws_url
# =============================================================================


def test_docker_runtime_endpoint_ws_url(tmp_path: Path) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    workspace_path = _workspace_dir(tmp_path)
    handle = backend.start(
        agent_id="agent-ws",
        workspace_path=workspace_path,
        image_tag="test",
    )

    endpoint = backend.endpoint(handle)

    assert endpoint.ws_url == "ws://127.0.0.1:18080/agent/sessions/{session_id}/ws"
    assert (
        endpoint.ws_endpoint("session-abc")
        == "ws://127.0.0.1:18080/agent/sessions/session-abc/ws"
    )
    assert endpoint.health_url == "http://127.0.0.1:18080/ping"


# =============================================================================
# unknown handle
# =============================================================================


def test_docker_runtime_unknown_handle_raises_runtime_not_found() -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    backend = DockerSandboxBackend(client=FakeDockerClient(FakeContainer()))

    with pytest.raises(DomainError) as exc_info:
        backend.status("missing-runtime")

    assert exc_info.value.code == "SANDBOX_NOT_FOUND"
    assert exc_info.value.details["sandbox_id"] == "missing-runtime"


# =============================================================================
# factory — create_sandbox_backend("docker")
# =============================================================================


def test_docker_runtime_factory_returns_backend() -> None:
    backend = create_sandbox_backend("docker")

    from witty_service.sandbox.docker import DockerSandboxBackend

    assert isinstance(backend, DockerSandboxBackend)


def test_docker_runtime_factory_injects_env_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITTY_DOCKER_IMAGE", "registry.example/witty-agent")
    monkeypatch.setenv("WITTY_DOCKER_CONTAINER_PORT", "19090")
    monkeypatch.setenv(
        "WITTY_DOCKER_CONTAINER_WORKSPACE_PATH",
        "/custom-workspace",
    )
    _reset_settings(monkeypatch)

    backend = create_sandbox_backend("docker")

    from witty_service.sandbox.docker import DockerSandboxBackend

    assert isinstance(backend, DockerSandboxBackend)
    assert backend.base_image == "registry.example/witty-agent"
    assert backend.container_port == 19090
    assert backend.container_workspace_path == "/custom-workspace"


def test_docker_runtime_factory_rejects_explicit_image_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 镜像 tag 现在由 start() 的 image_tag kwarg 动态拼接，
    # WITTY_DOCKER_IMAGE 必须是无 tag 的仓库名，带 tag 直接拒绝。
    monkeypatch.setenv("WITTY_DOCKER_IMAGE", "registry.example/witty-agent:v1")
    _reset_settings(monkeypatch)

    with pytest.raises(ValueError):
        create_sandbox_backend("docker")


def test_docker_runtime_factory_rejects_invalid_container_port_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITTY_DOCKER_CONTAINER_PORT", "not-a-number")
    _reset_settings(monkeypatch)

    with pytest.raises(ValueError):
        create_sandbox_backend("docker")


def test_docker_runtime_factory_rejects_invalid_stop_timeout_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITTY_DOCKER_STOP_TIMEOUT", "not-a-number")
    _reset_settings(monkeypatch)

    with pytest.raises(ValueError):
        create_sandbox_backend("docker")


# =============================================================================
# start() — docker run failure
# =============================================================================


def test_docker_runtime_start_raises_domain_error_when_run_fails(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    class RunErrorContainers(FakeContainers):
        def run(self, image: str, **kwargs: object) -> FakeContainer:
            raise RuntimeError("docker run failed")

    container = FakeContainer()
    client = FakeDockerClient(container)
    client.containers = RunErrorContainers(container)

    backend = DockerSandboxBackend(client=client, image="witty-agent")
    workspace_path = _workspace_dir(tmp_path)

    with pytest.raises(DomainError) as exc_info:
        backend.start(
            agent_id="agent-run-fail",
            workspace_path=workspace_path,
            port=18170,
            image_tag="test",
        )

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert exc_info.value.details["image"] == "witty-agent"


# =============================================================================
# start() — container_name_prefix customization
# =============================================================================


def test_docker_runtime_start_uses_custom_container_name_prefix(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)

    class CustomPrefixBackend(DockerSandboxBackend):
        CONTAINER_NAME_PREFIX = "custom-prefix"

    backend = CustomPrefixBackend(client=client, image="witty-agent")
    workspace_path = _workspace_dir(tmp_path)

    backend.start(
        agent_id="agent-custom-prefix",
        workspace_path=workspace_path,
        port=18180,
        image_tag="test",
    )

    assert client.containers.run_calls[0]["name"] == "custom-prefix-agent-custom-prefix"


# =============================================================================
# start() — image_tag kwarg
# =============================================================================


def test_docker_runtime_start_requires_image_tag(tmp_path: Path) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    backend = DockerSandboxBackend(client=FakeDockerClient(FakeContainer()))
    workspace_path = _workspace_dir(tmp_path)

    with pytest.raises(DomainError) as exc_info:
        backend.start(
            agent_id="agent-no-tag",
            workspace_path=workspace_path,
            port=18090,
        )

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert "image_tag" in exc_info.value.message


def test_docker_runtime_start_with_image_tag_concatenates_image(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client, image="my-registry/witty-agent")
    workspace_path = _workspace_dir(tmp_path)

    backend.start(
        agent_id="agent-custom-tag",
        workspace_path=workspace_path,
        port=18190,
        image_tag="v2.0.1",
    )

    assert client.containers.run_calls[0]["image"] == "my-registry/witty-agent:v2.0.1"


def test_docker_runtime_start_falls_back_to_default_base_image(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client)
    workspace_path = _workspace_dir(tmp_path)

    backend.start(
        agent_id="agent-default-image",
        workspace_path=workspace_path,
        port=18200,
        image_tag="latest",
    )

    assert (
        client.containers.run_calls[0]["image"]
        == "ghcr.io/openwitty/witty-agent-server:latest"
    )


# =============================================================================
# start() — memory_limit kwarg override
# =============================================================================


def test_docker_runtime_start_with_memory_limit_override(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client, memory_limit="256m")
    workspace_path = _workspace_dir(tmp_path)

    backend.start(
        agent_id="agent-mem-override",
        workspace_path=workspace_path,
        port=18210,
        image_tag="test",
        memory_limit="2g",
    )

    assert client.containers.run_calls[0]["mem_limit"] == "2g"


def test_docker_runtime_start_uses_instance_memory_limit_by_default(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client, memory_limit="1g")
    workspace_path = _workspace_dir(tmp_path)

    backend.start(
        agent_id="agent-mem-default",
        workspace_path=workspace_path,
        port=18220,
        image_tag="test",
    )

    assert client.containers.run_calls[0]["mem_limit"] == "1g"


# =============================================================================
# start() — env kwarg edge cases
# =============================================================================


def test_docker_runtime_start_with_env_parameter(tmp_path: Path) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client)
    workspace_path = _workspace_dir(tmp_path)

    backend.start(
        agent_id="agent-env-param",
        workspace_path=workspace_path,
        image_tag="test",
        env={"CUSTOM_VAR": "hello"},
    )

    assert client.containers.run_calls[0]["environment"] == {"CUSTOM_VAR": "hello"}


def test_docker_runtime_start_with_empty_env_not_overwritten(
    tmp_path: Path,
) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client)
    workspace_path = _workspace_dir(tmp_path)

    backend.start(
        agent_id="agent-empty-env",
        workspace_path=workspace_path,
        image_tag="test",
        env={},
    )

    assert client.containers.run_calls[0]["environment"] == {}


def test_docker_runtime_start_default_env_is_empty(tmp_path: Path) -> None:
    from witty_service.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client)
    workspace_path = _workspace_dir(tmp_path)

    backend.start(
        agent_id="agent-no-env",
        workspace_path=workspace_path,
        image_tag="test",
    )

    assert client.containers.run_calls[0]["environment"] == {}

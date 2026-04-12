from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.domain.errors import DomainError
from src.sandbox.base import SandboxStatus
from src.sandbox.factory import create_sandbox_backend


@dataclass
class FakeContainer:
    id: str = "container-123"
    status: str = "running"

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

    def run(self, image: str, **kwargs: object) -> FakeContainer:
        self.run_calls.append({"image": image, **kwargs})
        return self.container


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


def _workspace_dir(tmp_path: Path) -> str:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return str(workspace.resolve())


def test_docker_runtime_start_mounts_workspace_to_witty_workspace(
    tmp_path: Path,
) -> None:
    from src.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    client = FakeDockerClient(container)
    backend = DockerSandboxBackend(client=client, image="witty-agent:test")
    workspace_path = _workspace_dir(tmp_path)

    handle = backend.start(
        agent_id="agent-1",
        workspace_path=workspace_path,
        port=18080,
    )

    assert client.containers.run_calls == [
        {
            "image": "witty-agent:test",
            "detach": True,
            "ports": {"8080/tcp": 18080},
            "volumes": {
                workspace_path: {
                    "bind": "/witty-workspace",
                    "mode": "rw",
                }
            },
            "environment": {},
        }
    ]
    assert handle.workspace_path == workspace_path
    assert handle.metadata["container_id"] == "container-123"
    assert handle.metadata["host_port"] == 18080
    assert handle.metadata["base_url"] == "http://127.0.0.1:18080"
    assert handle.metadata["workspace_mount_path"] == "/witty-workspace"


def test_docker_runtime_stop_cleanup_and_endpoint(tmp_path: Path) -> None:
    from src.sandbox.docker import DockerSandboxBackend

    container = FakeContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    workspace_path = _workspace_dir(tmp_path)
    handle = backend.start(
        agent_id="agent-2",
        workspace_path=workspace_path,
        port=18081,
    )

    endpoint = backend.endpoint(handle)
    backend.stop(handle)
    backend.cleanup(handle)

    assert endpoint.base_url == "http://127.0.0.1:18081"
    assert endpoint.health_url == "http://127.0.0.1:18081/v1/ping"
    assert container.stop_called is True
    assert container.remove_called is True
    assert handle.sandbox_id not in backend._handles
    assert handle.sandbox_id not in backend._containers


@pytest.mark.parametrize(
    ("container_status", "expected_status"),
    [
        ("created", SandboxStatus.starting),
        ("running", SandboxStatus.running),
        ("exited", SandboxStatus.stopped),
        ("dead", SandboxStatus.error),
        ("mystery", SandboxStatus.error),
    ],
)
def test_docker_runtime_status_maps_container_state(
    tmp_path: Path,
    container_status: str,
    expected_status: SandboxStatus,
) -> None:
    from src.sandbox.docker import DockerSandboxBackend

    container = FakeContainer(status=container_status)
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    workspace_path = _workspace_dir(tmp_path)
    handle = backend.start(
        agent_id="agent-3",
        workspace_path=workspace_path,
        port=18082,
    )

    assert backend.status(handle) is expected_status
    assert container.reload_called is True


def test_docker_runtime_status_raises_domain_error_when_docker_api_fails(
    tmp_path: Path,
) -> None:
    from src.sandbox.docker import DockerSandboxBackend

    container = ReloadErrorContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    handle = backend.start(
        agent_id="agent-4",
        workspace_path=_workspace_dir(tmp_path),
        port=18083,
    )

    with pytest.raises(DomainError) as exc_info:
        backend.status(handle)

    assert exc_info.value.code == "SANDBOX_STOP_FAILED"
    assert exc_info.value.details["sandbox_id"] == handle.sandbox_id
    assert exc_info.value.details["container_id"] == "container-123"


def test_docker_runtime_cleanup_attempts_remove_when_stop_fails(
    tmp_path: Path,
) -> None:
    from src.sandbox.docker import DockerSandboxBackend

    container = StopErrorContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    handle = backend.start(
        agent_id="agent-5",
        workspace_path=_workspace_dir(tmp_path),
        port=18084,
    )

    with pytest.raises(DomainError) as exc_info:
        backend.cleanup(handle)

    assert exc_info.value.code == "SANDBOX_STOP_FAILED"
    assert container.stop_called is True
    assert container.remove_called is True
    assert handle.sandbox_id in backend._handles
    assert handle.sandbox_id in backend._containers


def test_docker_runtime_stop_raises_domain_error_when_docker_api_fails(
    tmp_path: Path,
) -> None:
    from src.sandbox.docker import DockerSandboxBackend

    container = StopErrorContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    handle = backend.start(
        agent_id="agent-stop",
        workspace_path=_workspace_dir(tmp_path),
        port=18086,
    )

    with pytest.raises(DomainError) as exc_info:
        backend.stop(handle)

    assert exc_info.value.code == "SANDBOX_STOP_FAILED"
    assert exc_info.value.details["sandbox_id"] == handle.sandbox_id
    assert exc_info.value.details["container_id"] == "container-123"
    assert container.stop_called is True


def test_docker_runtime_stop_raises_domain_error_when_remove_fails_in_cleanup(
    tmp_path: Path,
) -> None:
    from src.sandbox.docker import DockerSandboxBackend

    container = RemoveErrorContainer()
    backend = DockerSandboxBackend(client=FakeDockerClient(container))
    handle = backend.start(
        agent_id="agent-6",
        workspace_path=_workspace_dir(tmp_path),
        port=18085,
    )

    with pytest.raises(DomainError) as exc_info:
        backend.cleanup(handle)

    assert exc_info.value.code == "SANDBOX_STOP_FAILED"
    assert container.stop_called is True
    assert container.remove_called is True
    assert handle.sandbox_id in backend._handles
    assert handle.sandbox_id in backend._containers


def test_docker_runtime_start_rejects_invalid_workspace_path() -> None:
    from src.sandbox.docker import DockerSandboxBackend

    backend = DockerSandboxBackend(client=FakeDockerClient(FakeContainer()))

    with pytest.raises(DomainError) as exc_info:
        backend.start(agent_id="agent-7", workspace_path="relative/workspace")

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert exc_info.value.details["path"] == "relative/workspace"


def test_docker_runtime_start_rejects_missing_workspace_path(tmp_path: Path) -> None:
    from src.sandbox.docker import DockerSandboxBackend

    backend = DockerSandboxBackend(client=FakeDockerClient(FakeContainer()))
    missing_path = str((tmp_path / "missing").resolve())

    with pytest.raises(DomainError) as exc_info:
        backend.start(agent_id="agent-8", workspace_path=missing_path)

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert exc_info.value.details["path"] == missing_path


def test_docker_runtime_factory_returns_backend() -> None:
    backend = create_sandbox_backend("docker")

    from src.sandbox.docker import DockerSandboxBackend

    assert isinstance(backend, DockerSandboxBackend)


def test_docker_runtime_factory_injects_env_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITTY_DOCKER_IMAGE", "registry.example/witty-agent")
    monkeypatch.setenv("WITTY_DOCKER_IMAGE_TAG", "v2")
    monkeypatch.setenv("WITTY_DOCKER_CONTAINER_PORT", "19090")
    monkeypatch.setenv(
        "WITTY_DOCKER_CONTAINER_WORKSPACE_PATH",
        "/custom-workspace",
    )

    backend = create_sandbox_backend("docker")

    from src.sandbox.docker import DockerSandboxBackend

    assert isinstance(backend, DockerSandboxBackend)
    assert backend.image == "registry.example/witty-agent:v2"
    assert backend.container_port == 19090
    assert backend.container_workspace_path == "/custom-workspace"
    assert backend.workspace_mount_path == "/custom-workspace"


def test_docker_runtime_factory_keeps_explicit_image_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITTY_DOCKER_IMAGE", "registry.example/witty-agent:v1")
    monkeypatch.setenv("WITTY_DOCKER_IMAGE_TAG", "latest")

    backend = create_sandbox_backend("docker")

    from src.sandbox.docker import DockerSandboxBackend

    assert isinstance(backend, DockerSandboxBackend)
    assert backend.image == "registry.example/witty-agent:v1"


def test_docker_runtime_factory_rejects_invalid_container_port_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITTY_DOCKER_CONTAINER_PORT", "not-a-number")

    with pytest.raises(DomainError) as exc_info:
        create_sandbox_backend("docker")

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert exc_info.value.details["env_var"] == "WITTY_DOCKER_CONTAINER_PORT"


def test_docker_runtime_factory_rejects_invalid_stop_timeout_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WITTY_DOCKER_STOP_TIMEOUT", "not-a-number")

    with pytest.raises(DomainError) as exc_info:
        create_sandbox_backend("docker")

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert exc_info.value.details["env_var"] == "WITTY_DOCKER_STOP_TIMEOUT"


def test_docker_runtime_unknown_handle_raises_runtime_not_found() -> None:
    from src.sandbox.docker import DockerSandboxBackend

    backend = DockerSandboxBackend(client=FakeDockerClient(FakeContainer()))

    with pytest.raises(DomainError) as exc_info:
        backend.status("missing-runtime")

    assert exc_info.value.code == "SANDBOX_NOT_FOUND"
    assert exc_info.value.details["sandbox_id"] == "missing-runtime"

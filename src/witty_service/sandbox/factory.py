from __future__ import annotations

from collections.abc import Callable

from witty_service.config import get_settings
from witty_service.domain.errors import DomainError
from witty_service.sandbox.base import SANDBOX_NOT_SUPPORTED
from witty_service.sandbox.base import SandboxBackend
from witty_service.sandbox.base import sandbox_start_failed
from witty_service.sandbox.docker import DockerSandboxBackend
from witty_service.sandbox.e2b import E2BSandboxBackend
from witty_service.sandbox.local_process import LocalProcessSandboxBackend

SandboxBackendFactory = Callable[[], SandboxBackend]


def _create_docker_backend() -> DockerSandboxBackend:
    settings = get_settings()
    return DockerSandboxBackend(
        image=settings.docker.image,
        host=settings.docker.host,
        container_port=settings.docker.container_port,
        container_workspace_path=settings.docker.container_workspace_path,
        stop_timeout=settings.docker.stop_timeout,
        memory_limit=settings.docker.memory_limit,
        pids_limit=settings.docker.pids_limit,
        cpu_shares=settings.docker.cpu_shares,
        nofile_soft_limit=settings.docker.nofile_soft_limit,
        nofile_hard_limit=settings.docker.nofile_hard_limit,
        tmpfs_size=settings.docker.tmpfs_size,
        read_only=settings.docker.read_only,
    )


def _create_local_process_backend() -> LocalProcessSandboxBackend:
    settings = get_settings()
    return LocalProcessSandboxBackend(
        agent_server_app_dir=settings.workspace.agent_server_app_dir
    )


_SANDBOX_BACKEND_FACTORIES: dict[str, SandboxBackendFactory] = {
    "docker": _create_docker_backend,
    "e2b": E2BSandboxBackend,
    "local_process": _create_local_process_backend,
}


def register_sandbox_backend(sandbox_type: str, factory: SandboxBackendFactory) -> None:
    _SANDBOX_BACKEND_FACTORIES[sandbox_type.lower()] = factory


def create_sandbox_backend(sandbox_type: str) -> SandboxBackend:
    try:
        factory = _SANDBOX_BACKEND_FACTORIES[sandbox_type.lower()]
    except KeyError as exc:
        raise DomainError(
            code=SANDBOX_NOT_SUPPORTED,
            message="Sandbox backend is not supported yet.",
            details={
                "sandbox_type": sandbox_type,
                "operation": "create",
            },
        ) from exc
    return factory()




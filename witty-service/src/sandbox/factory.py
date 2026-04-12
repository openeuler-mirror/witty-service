from __future__ import annotations

import os
from collections.abc import Callable

from src.domain.errors import DomainError
from src.sandbox.base import SANDBOX_NOT_SUPPORTED
from src.sandbox.base import SandboxBackend
from src.sandbox.base import sandbox_start_failed
from src.sandbox.docker import DockerSandboxBackend
from src.sandbox.e2b import E2BSandboxBackend
from src.sandbox.local_process import LocalProcessSandboxBackend

SandboxBackendFactory = Callable[[], SandboxBackend]

_SANDBOX_BACKEND_FACTORIES: dict[str, SandboxBackendFactory] = {
    "docker": lambda: DockerSandboxBackend(
        image=_docker_image_from_env(),
        host=os.getenv("WITTY_DOCKER_HOST", "127.0.0.1"),
        container_port=_docker_int_env("WITTY_DOCKER_CONTAINER_PORT", 8080),
        container_workspace_path=os.getenv(
            "WITTY_DOCKER_CONTAINER_WORKSPACE_PATH",
            "/witty-workspace",
        ),
        stop_timeout=_docker_int_env("WITTY_DOCKER_STOP_TIMEOUT", 10),
    ),
    "e2b": E2BSandboxBackend,
    "local_process": lambda: LocalProcessSandboxBackend(
        agent_server_app_dir=os.getenv("WITTY_AGENT_SERVER_APP_DIR")
    ),
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


def _docker_image_from_env() -> str:
    image = os.getenv("WITTY_DOCKER_IMAGE", "witty-agent-server")
    tag = os.getenv("WITTY_DOCKER_IMAGE_TAG", "latest")
    if "@" in image or _docker_image_has_explicit_tag(image):
        return image
    return f"{image}:{tag}"


def _docker_image_has_explicit_tag(image: str) -> bool:
    last_segment = image.rsplit("/", 1)[-1]
    return ":" in last_segment


def _docker_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise sandbox_start_failed(
            sandbox_type="docker",
            message=f"Invalid docker sandbox env: {name} must be an integer.",
            details={
                "env_var": name,
                "value": raw_value,
            },
        ) from exc

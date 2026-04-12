from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.sandbox.base import (
    AdapterEndpoint,
    SandboxBackend,
    SandboxHandle,
    SandboxStatus,
    sandbox_not_found,
    sandbox_start_failed,
    sandbox_stop_failed,
)
from src.sandbox.local_process import find_free_port

DEFAULT_DOCKER_IMAGE = "witty-agent-server:latest"
DEFAULT_CONTAINER_PORT = 8080
# 默认契约必须保持为 /witty-workspace，除非显式配置 container_workspace_path。
DEFAULT_CONTAINER_WORKSPACE_PATH = "/witty-workspace"


class DockerSandboxBackend(SandboxBackend):
    sandbox_type = "docker"

    def __init__(
        self,
        *,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        image: str = DEFAULT_DOCKER_IMAGE,
        host: str = "127.0.0.1",
        container_port: int = DEFAULT_CONTAINER_PORT,
        container_workspace_path: str = DEFAULT_CONTAINER_WORKSPACE_PATH,
        workspace_mount_path: str | None = None,
        stop_timeout: int = 10,
    ) -> None:
        self._client = client
        self._client_factory = client_factory or _create_default_client
        self.image = image
        self.host = host
        self.container_port = container_port
        self.container_workspace_path = (
            workspace_mount_path or container_workspace_path
        )
        self.workspace_mount_path = self.container_workspace_path
        self.stop_timeout = stop_timeout
        self._handles: dict[str, SandboxHandle] = {}
        self._containers: dict[str, Any] = {}

    def start(
        self,
        *,
        agent_id: str,
        workspace_path: str,
        **kwargs: Any,
    ) -> SandboxHandle:
        resolved_workspace_path = self._validate_workspace_path(workspace_path)
        host_port = int(kwargs.get("port", find_free_port()))
        environment = dict(kwargs.get("environment", {}))

        try:
            container = self._get_client().containers.run(
                self.image,
                detach=True,
                ports={f"{self.container_port}/tcp": host_port},
                volumes={
                    resolved_workspace_path: {
                        "bind": self.container_workspace_path,
                        "mode": "rw",
                    }
                },
                environment=environment,
            )
        except Exception as exc:
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Failed to start docker sandbox.",
                details={
                    "image": self.image,
                    "path": workspace_path,
                    "container_workspace_path": self.container_workspace_path,
                    "host_port": host_port,
                    "stderr": str(exc),
                },
            ) from exc

        sandbox_id = str(uuid4())
        base_url = f"http://{self.host}:{host_port}"
        handle = SandboxHandle(
            sandbox_id=sandbox_id,
            agent_id=agent_id,
            workspace_path=resolved_workspace_path,
            metadata={
                "container_id": str(container.id),
                "host_port": host_port,
                "base_url": base_url,
                "image": self.image,
                "container_port": self.container_port,
                "container_workspace_path": self.container_workspace_path,
                "workspace_mount_path": self.container_workspace_path,
            },
        )
        self._handles[sandbox_id] = handle
        self._containers[sandbox_id] = container
        return handle

    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        sandbox_handle = self._resolve_handle(handle)
        container = self._containers.get(sandbox_handle.sandbox_id)
        if container is None:
            return
        try:
            container.stop(timeout=int(kwargs.get("timeout", self.stop_timeout)))
        except Exception as exc:
            raise self._sandbox_operation_failed(
                operation="stop",
                sandbox_handle=sandbox_handle,
                container=container,
                error=exc,
            ) from exc

    def status(self, handle: SandboxHandle | str, **kwargs: Any) -> SandboxStatus:
        sandbox_handle = self._resolve_handle(handle)
        container = self._containers.get(sandbox_handle.sandbox_id)
        if container is None:
            return SandboxStatus.stopped

        try:
            container.reload()
        except Exception as exc:
            raise self._sandbox_operation_failed(
                operation="status",
                sandbox_handle=sandbox_handle,
                container=container,
                error=exc,
            ) from exc
        return _map_container_status(getattr(container, "status", "unknown"))

    def endpoint(
        self, handle: SandboxHandle | str, **kwargs: Any
    ) -> AdapterEndpoint:
        sandbox_handle = self._resolve_handle(handle)
        base_url = str(sandbox_handle.metadata["base_url"])
        return AdapterEndpoint(base_url=base_url, health_url=f"{base_url}/v1/ping")

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        sandbox_handle = self._resolve_handle(handle)
        container = self._containers.get(sandbox_handle.sandbox_id)
        stop_error: Exception | None = None
        remove_error: Exception | None = None
        if container is not None:
            try:
                container.stop(timeout=int(kwargs.get("timeout", self.stop_timeout)))
            except Exception as exc:
                stop_error = exc
            try:
                container.remove(force=bool(kwargs.get("force", False)))
            except Exception as exc:
                remove_error = exc
        if stop_error or remove_error:
            raise self._sandbox_operation_failed(
                operation="cleanup",
                sandbox_handle=sandbox_handle,
                container=container,
                error=remove_error or stop_error or RuntimeError("cleanup failed"),
                extra_details={
                    "stop_error": str(stop_error) if stop_error else None,
                    "remove_error": str(remove_error) if remove_error else None,
                },
            )
        self._containers.pop(sandbox_handle.sandbox_id, None)
        self._handles.pop(sandbox_handle.sandbox_id, None)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _validate_workspace_path(self, workspace_path: str) -> str:
        path = Path(workspace_path).expanduser()
        if not path.is_absolute():
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Docker sandbox requires an absolute workspace path.",
                details={"path": workspace_path},
            )
        if not path.exists():
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Docker sandbox workspace path does not exist.",
                details={"path": workspace_path},
            )
        if not path.is_dir():
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Docker sandbox workspace path must be a directory.",
                details={"path": workspace_path},
            )
        return str(path.resolve())

    def _resolve_handle(self, handle: SandboxHandle | str) -> SandboxHandle:
        sandbox_id = handle.sandbox_id if isinstance(handle, SandboxHandle) else handle
        try:
            return self._handles[sandbox_id]
        except KeyError as exc:
            raise sandbox_not_found(
                sandbox_type=self.sandbox_type,
                sandbox_id=sandbox_id,
            ) from exc

    def _sandbox_operation_failed(
        self,
        *,
        operation: str,
        sandbox_handle: SandboxHandle,
        container: Any | None,
        error: Exception,
        extra_details: dict[str, Any] | None = None,
    ):
        details = {
            "sandbox_id": sandbox_handle.sandbox_id,
            "container_id": self._container_id(container),
            "operation": operation,
            "error": str(error),
        }
        if extra_details:
            details.update(extra_details)
        return sandbox_stop_failed(
            sandbox_type=self.sandbox_type,
            message=f"Failed to {operation} docker sandbox.",
            details=details,
        )

    @staticmethod
    def _container_id(container: Any | None) -> str | None:
        if container is None:
            return None
        container_id = getattr(container, "id", None)
        return str(container_id) if container_id is not None else None


def _map_container_status(status: str) -> SandboxStatus:
    mapping = {
        "created": SandboxStatus.starting,
        "restarting": SandboxStatus.starting,
        "running": SandboxStatus.running,
        "exited": SandboxStatus.stopped,
        "paused": SandboxStatus.stopped,
        "removing": SandboxStatus.stopped,
    }
    return mapping.get(status, SandboxStatus.error)


def _create_default_client() -> Any:
    try:
        import docker  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise sandbox_start_failed(
            sandbox_type=DockerSandboxBackend.sandbox_type,
            message="Docker sandbox requires the docker SDK to be installed.",
            details={"package": "docker"},
        ) from exc

    return docker.from_env()

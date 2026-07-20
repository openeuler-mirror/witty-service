from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from docker.errors import APIError, NotFound
from requests.exceptions import ConnectionError

from witty_service.sandbox.base import (
    AdapterEndpoint,
    SandboxBackend,
    SandboxHandle,
    SandboxStatus,
    sandbox_not_found,
    sandbox_start_failed,
    sandbox_stop_failed,
)
logger = logging.getLogger(__name__)

DEFAULT_DOCKER_BASE_IMAGE = "ghcr.io/openwitty/witty-agent-server"
DEFAULT_CONTAINER_PORT = 8080
# 默认契约必须保持为 /witty-workspace，除非显式配置 container_workspace_path。
DEFAULT_CONTAINER_WORKSPACE_PATH = "/witty-workspace"


def _with_retry(operation, *, max_retries: int = 3, base_delay: float = 0.5):
    """
    在遇到可恢复的 Docker 错误时，以指数退避策略调用 operation。
    该方法处理以下异常：
        NotFound — 立即重新抛出（缺失的容器无法通过等待修复）
        APIError（NotFound 除外）— 以指数退避策略重试
        ConnectionError — 以指数退避策略重试（守护进程重启、socket 超时等情况）
    """
    import time as _time

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            return operation()
        except NotFound:
            raise
        except (APIError, ConnectionError) as exc:
            last_error = exc
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt)
                logger.warning(
                    "Docker API error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1,
                    max_retries,
                    delay,
                    exc,
                )
                _time.sleep(delay)
    raise last_error  # type: ignore[misc]


class DockerSandboxBackend(SandboxBackend):
    sandbox_type = "docker"

    CONTAINER_NAME_PREFIX = "witty-sandbox"

    def __init__(
        self,
        *,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        image: str = DEFAULT_DOCKER_BASE_IMAGE,
        host: str = "127.0.0.1",
        container_port: int = DEFAULT_CONTAINER_PORT,
        container_workspace_path: str = DEFAULT_CONTAINER_WORKSPACE_PATH,
        workspace_mount_path: str | None = None,
        stop_timeout: int = 10,
        memory_limit: str = "512m",
        pids_limit: int = 100,
        cpu_shares: int = 512,
        nofile_soft_limit: int = 1024,
        nofile_hard_limit: int = 4096,
        tmpfs_size: str = "256M",
        read_only: bool = True,
    ) -> None:
        self._client = client
        self._client_factory = client_factory or _create_default_client
        self.base_image = image
        self.host = host
        self.container_port = container_port
        self.container_workspace_path = (
            workspace_mount_path or container_workspace_path
        )
        self.stop_timeout = stop_timeout
        self.memory_limit = memory_limit
        self.pids_limit = pids_limit
        self.cpu_shares = cpu_shares
        self.nofile_soft_limit = nofile_soft_limit
        self.nofile_hard_limit = nofile_hard_limit
        self.tmpfs_size = tmpfs_size
        self.read_only = read_only
        self._handles: dict[str, SandboxHandle] = {}
        self._containers: dict[str, Any] = {}

    def start(
        self,
        *,
        agent_id: str,
        workspace_path: str,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> SandboxHandle:
        resolved_workspace_path = self._validate_workspace_path(workspace_path)

        existing_handle = self._find_handle_by_agent_id(agent_id)
        if existing_handle is not None:
            return existing_handle

        container_name = f"{self.CONTAINER_NAME_PREFIX}-{agent_id}"
        existing_container = self._try_find_container(container_name)
        if existing_container is not None:
            handle = self._build_handle_from_container(
                existing_container, agent_id, resolved_workspace_path
            )
            handle = SandboxHandle(
                sandbox_id=handle.sandbox_id,
                agent_id=handle.agent_id,
                workspace_path=handle.workspace_path,
                metadata={**handle.metadata, "reconnected": True},
            )
            self._handles[handle.sandbox_id] = handle
            return handle

        image_tag = kwargs.get("image_tag")
        if not image_tag:
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Docker sandbox requires 'image_tag' kwarg.",
                details={"kwargs_keys": list(kwargs.keys())},
            )
        image = f"{self.base_image}:{image_tag}"

        environment = dict(env or {})

        # 允许通过 kwargs 按启动覆盖内存限制，否则回退到实例默认值
        memory_limit = kwargs.get("memory_limit", self.memory_limit)

        try:
            container = self._get_client().containers.run(
                image,
                name=container_name,
                detach=True,
                user="witty",
                ports={f"{self.container_port}/tcp": None},
                volumes={
                    resolved_workspace_path: {
                        "bind": self.container_workspace_path,
                        "mode": "rw",
                    },
                },
                environment=environment,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit=memory_limit,
                pids_limit=self.pids_limit,
                cpu_shares=self.cpu_shares,
                read_only=self.read_only,
                tmpfs={
                    "/tmp": f"rw,noexec,nosuid,size={self.tmpfs_size}",
                    "/home/witty": f"rw,nosuid,uid=1000,gid=1000,mode=0755,size={self.tmpfs_size}",
                },
                ulimits=[
                    {"name": "nofile", "soft": self.nofile_soft_limit, "hard": self.nofile_hard_limit},
                ],
            )
        except Exception as exc:
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Failed to start docker sandbox.",
                details={
                    "image": self.base_image,
                    "path": workspace_path,
                    "container_workspace_path": self.container_workspace_path,
                    "stderr": str(exc),
                },
            ) from exc

        return self._build_handle_from_container(
            container, agent_id, resolved_workspace_path
        )

    def _find_handle_by_agent_id(self, agent_id: str) -> SandboxHandle | None:
        for handle in self._handles.values():
            if handle.agent_id == agent_id:
                return handle
        return None

    def _try_find_container(self, container_name: str) -> Any | None:
        try:
            container = _with_retry(
                lambda: self._get_client().containers.get(container_name)
            )
        except NotFound:
            return None

        try:
            _with_retry(lambda: container.reload())
        except (NotFound, APIError, ConnectionError):
            return None

        if container.status == "running":
            return container

        try:
            container.remove(force=True)
        except (NotFound, APIError):
            pass
        return None

    def _build_handle_from_container(
        self,
        container: Any,
        agent_id: str,
        workspace_path: str,
        host_port: int | None = None,
    ) -> SandboxHandle:
        if host_port is None:
            host_port = self._extract_host_port(container)
        base_url = f"http://{self.host}:{host_port}"
        sandbox_id = str(uuid4())
        handle = SandboxHandle(
            sandbox_id=sandbox_id,
            agent_id=agent_id,
            workspace_path=workspace_path,
            metadata={
                "container_id": str(container.id),
                "host_port": host_port,
                "base_url": base_url,
                "image": self.base_image,
                "container_port": self.container_port,
                "container_workspace_path": self.container_workspace_path,
            },
        )
        self._handles[sandbox_id] = handle
        self._containers[sandbox_id] = container
        return handle

    def _extract_host_port(self, container: Any) -> int:
        port_key = f"{self.container_port}/tcp"
        try:
            ports = container.attrs["NetworkSettings"]["Ports"]
            # containers.run() 后需 reload() 刷新端口绑定；已重连的容器跳过。
            if port_key not in (ports or {}):
                _with_retry(lambda: container.reload())
                ports = container.attrs["NetworkSettings"]["Ports"]
            return int(ports[port_key][0]["HostPort"])
        except (KeyError, IndexError, TypeError, NotFound, APIError, ConnectionError) as exc:
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Failed to extract host port from container.",
                details={
                    "container_id": self._container_id(container) or "unknown",
                    "port_key": port_key,
                    "error": str(exc),
                },
            ) from exc

    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None:

        sandbox_handle = self._resolve_handle(handle)
        container = self._containers.get(sandbox_handle.sandbox_id)
        if container is None:
            return
        try:
            _with_retry(
                lambda: container.stop(
                    timeout=int(kwargs.get("timeout", self.stop_timeout))
                )
            )
        except NotFound:
            return
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
            _with_retry(lambda: container.reload())
        except NotFound:
            return SandboxStatus.stopped
        except Exception as exc:
            raise self._sandbox_operation_failed(
                operation="status",
                sandbox_handle=sandbox_handle,
                container=container,
                error=exc,
            ) from exc

        raw_status = getattr(container, "status", "unknown")
        if raw_status == "exited":
            state = container.attrs.get("State", {}) if hasattr(container, "attrs") else {}
            if state.get("OOMKilled", False):
                logger.warning(
                    "Container %s was killed by OOM killer (exit_code=%s)",
                    self._container_id(container),
                    state.get("ExitCode", "unknown"),
                )
        return _map_container_status(raw_status)

    def endpoint(
        self, handle: SandboxHandle | str, **kwargs: Any
    ) -> AdapterEndpoint:
        sandbox_handle = self._resolve_handle(handle)
        base_url = str(sandbox_handle.metadata["base_url"])
        return AdapterEndpoint(base_url=base_url, health_url=f"{base_url}/ping")

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None:

        sandbox_handle = self._resolve_handle(handle)
        container = self._containers.get(sandbox_handle.sandbox_id)
        stop_error: Exception | None = None
        remove_error: Exception | None = None
        if container is not None:
            try:
                _with_retry(
                    lambda: container.stop(
                        timeout=int(kwargs.get("timeout", self.stop_timeout))
                    )
                )
            except NotFound:
                pass
            except Exception as exc:
                stop_error = exc
            try:
                _with_retry(
                    lambda: container.remove(
                        force=bool(kwargs.get("force", False))
                    )
                )
            except NotFound:
                pass
            except Exception as exc:
                remove_error = exc
        self._containers.pop(sandbox_handle.sandbox_id, None)
        self._handles.pop(sandbox_handle.sandbox_id, None)
        if stop_error or remove_error:
            raise self._sandbox_operation_failed(
                operation="cleanup",
                sandbox_handle=sandbox_handle,
                container=container,
                error=remove_error or stop_error,
                extra_details={
                    "stop_error": str(stop_error) if stop_error else None,
                    "remove_error": str(remove_error) if remove_error else None,
                },
            )

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

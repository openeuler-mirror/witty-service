"""Agent Sandbox Backend for Agent Middleware Service.

This module provides the SandboxBackend implementation for the Agent service.
It creates and manages sandbox container environments.

Design reference: Section 6.2 (SandboxBackend interface).

SandboxBackend Interface:
- start(sandbox_type, workspace_mount, adapter_config, options) -> SandboxInfo
- stop(sandbox_id) -> None
- pause(sandbox_id) -> None
- resume(sandbox_id) -> None

Note: Communication with the Adapter is handled by AgentManager via AdapterClient,
not by SandboxBackend. This separation of concerns ensures:
- SandboxBackend: Container lifecycle management only
- AgentManager: Adapter communication via AdapterClient
"""

import asyncio
import json
import logging
import os
import random
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _find_free_tcp_port(min_port: int, max_port: int) -> int:
    """Pick an available host TCP port in [min_port, max_port].

    Uses a local bind probe (small race window before Docker claims the port).
    Order is shuffled to reduce collisions when many sandboxes start at once.
    """
    if min_port > max_port:
        min_port, max_port = max_port, min_port
    ports = list(range(min_port, max_port + 1))
    random.shuffle(ports)
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free TCP port in range {min_port}-{max_port}")


class SandboxType(str, Enum):
    """Supported sandbox types for Agent service."""
    DOCKER = "docker"
    E2B = "e2b"
    OPENSANDBOX = "opensandbox"


class SandboxStatus(str, Enum):
    """Sandbox status for Agent service."""
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@dataclass
class WorkspaceMount:
    """Workspace mount information."""
    host_path: str
    guest_path: str = "/workspace"


@dataclass
class SandboxInfo:
    """Sandbox information."""
    id: str
    status: SandboxStatus
    workspace_mount: WorkspaceMount
    adapter_url: Optional[str] = None
    created_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


class AgentSandboxBackend(ABC):
    """Abstract base class for Agent sandbox backends.

    This class provides the interface for creating and managing sandboxes
    that run AgentAdapter instances. Each sandbox runs exactly one Agent.

    The backend communicates with the Adapter inside the sandbox via REST API
    to start/stop/send messages to the Agent.
    """

    @abstractmethod
    async def start(
        self,
        sandbox_type: str,
        workspace_mount: WorkspaceMount,
        adapter_config: dict,
        options: dict,
    ) -> SandboxInfo:
        """Start a new sandbox with an AgentAdapter.

        Args:
            sandbox_type: Type of sandbox (docker/e2b/opensandbox)
            workspace_mount: Workspace mount information
            adapter_config: Configuration for the adapter
            options: Additional sandbox-specific options

        Returns:
            SandboxInfo with sandbox ID and connection details
        """
        pass

    @abstractmethod
    async def stop(self, sandbox_id: str) -> None:
        """Stop a sandbox.

        Args:
            sandbox_id: The sandbox ID to stop
        """
        pass

    @abstractmethod
    async def pause(self, sandbox_id: str) -> None:
        """Pause a sandbox.

        Args:
            sandbox_id: The sandbox ID to pause
        """
        pass

    @abstractmethod
    async def resume(self, sandbox_id: str) -> SandboxInfo:
        """Resume a paused sandbox.

        Args:
            sandbox_id: The sandbox ID to resume

        Returns:
            SandboxInfo with updated status
        """
        pass

    @abstractmethod
    async def get_status(self, sandbox_id: str) -> SandboxStatus:
        """Get sandbox status.

        Args:
            sandbox_id: The sandbox ID

        Returns:
            Current SandboxStatus
        """
        pass


class DockerAgentSandboxBackend(AgentSandboxBackend):
    """Docker implementation of AgentSandboxBackend.

    This backend creates and manages Docker containers that run the Adapter.
    Container lifecycle is managed here, but adapter communication is handled
    by AgentManager via AdapterClient.

    Responsibilities:
    - Create/remove Docker containers
    - Pause/unpause containers
    - Provide adapter URL for AgentManager to use
    """

    def __init__(self):
        """Initialize Docker backend."""
        self._sandboxes: dict[str, SandboxInfo] = {}
        self._containers: dict[str, Any] = {}

    async def start(
        self,
        sandbox_type: str,
        workspace_mount: WorkspaceMount,
        adapter_config: dict,
        options: dict,
    ) -> SandboxInfo:
        """Start a Docker sandbox container.

        This method only starts the container. The AgentManager is responsible
        for calling the adapter's start endpoint via AdapterClient after
        the container is running.

        Args:
            sandbox_type: Must be "docker"
            workspace_mount: Workspace to mount into container
            adapter_config: Adapter configuration (passed as env var to container)
            options: Docker-specific options (image, etc.)

        Returns:
            SandboxInfo with sandbox details including adapter_url
        """
        import uuid

        sandbox_id = str(uuid.uuid4())
        image = options.get("image", "agentd/opencode-sandbox:latest")
        container_name = f"agent-sandbox-{sandbox_id}"

        port_min = int(
            options.get(
                "host_port_min",
                os.getenv("AGENT_SANDBOX_HOST_PORT_MIN", "22000"),
            )
        )
        port_max = int(
            options.get(
                "host_port_max",
                os.getenv("AGENT_SANDBOX_HOST_PORT_MAX", "29999"),
            )
        )
        host_port = _find_free_tcp_port(port_min, port_max)

        try:
            import docker
            client = docker.from_env()

            container = client.containers.run(
                image=image,
                name=container_name,
                detach=True,
                volumes={
                    workspace_mount.host_path: {
                        "bind": workspace_mount.guest_path,
                        "mode": "rw",
                    }
                },
                environment={
                    "ADAPTER_CONFIG": json.dumps(adapter_config),
                    "AGENT_WORKSPACE": workspace_mount.guest_path,
                },
                ports={
                    "8000/tcp": host_port,
                },
            )

            await asyncio.sleep(2)

            container.reload()
            adapter_url = f"http://localhost:{host_port}"

            sandbox_info = SandboxInfo(
                id=sandbox_id,
                status=SandboxStatus.RUNNING,
                workspace_mount=workspace_mount,
                adapter_url=adapter_url,
            )
            self._sandboxes[sandbox_id] = sandbox_info
            self._containers[sandbox_id] = container

            logger.info(f"Started Docker sandbox {sandbox_id} with adapter at {adapter_url}")
            return sandbox_info

        except Exception as e:
            logger.error(f"Failed to start Docker sandbox: {e}")
            raise

    async def stop(self, sandbox_id: str) -> None:
        """Stop and remove a Docker sandbox.

        This method stops the container. The AgentManager is responsible for
        calling the adapter's stop endpoint via AdapterClient before this.

        Args:
            sandbox_id: The sandbox ID to stop
        """
        if sandbox_id not in self._sandboxes:
            return

        sandbox_info = self._sandboxes[sandbox_id]

        if sandbox_id in self._containers:
            try:
                container = self._containers[sandbox_id]
                container.stop(timeout=10)
                container.remove()
                logger.info(f"Stopped and removed container for sandbox {sandbox_id}")
            except Exception as e:
                logger.warning(f"Failed to stop container: {e}")
            del self._containers[sandbox_id]

        sandbox_info.status = SandboxStatus.STOPPED
        del self._sandboxes[sandbox_id]

    async def pause(self, sandbox_id: str) -> None:
        """Pause a Docker sandbox container.

        This method pauses the container. The AgentManager is responsible for
        calling the adapter's stop endpoint via AdapterClient before this.

        Args:
            sandbox_id: The sandbox ID to pause
        """
        if sandbox_id not in self._sandboxes:
            return

        sandbox_info = self._sandboxes[sandbox_id]

        if sandbox_id in self._containers:
            try:
                container = self._containers[sandbox_id]
                container.pause()
                logger.info(f"Paused container for sandbox {sandbox_id}")
            except Exception as e:
                logger.warning(f"Failed to pause container: {e}")

        sandbox_info.status = SandboxStatus.PAUSED

    async def resume(self, sandbox_id: str) -> SandboxInfo:
        """Resume a paused Docker sandbox container.

        This method unpauses the container. The AgentManager is responsible
        for calling the adapter's start endpoint via AdapterClient after this.

        Args:
            sandbox_id: The sandbox ID to resume

        Returns:
            Updated SandboxInfo
        """
        if sandbox_id not in self._sandboxes:
            raise ValueError(f"Sandbox not found: {sandbox_id}")

        sandbox_info = self._sandboxes[sandbox_id]

        if sandbox_id in self._containers:
            try:
                container = self._containers[sandbox_id]
                container.unpause()
                logger.info(f"Resumed container for sandbox {sandbox_id}")
            except Exception as e:
                logger.warning(f"Failed to unpause container: {e}")

        sandbox_info.status = SandboxStatus.RUNNING
        return sandbox_info

    async def get_status(self, sandbox_id: str) -> SandboxStatus:
        """Get sandbox status.

        Args:
            sandbox_id: The sandbox ID

        Returns:
            Current SandboxStatus
        """
        if sandbox_id not in self._sandboxes:
            return SandboxStatus.STOPPED

        return self._sandboxes[sandbox_id].status


class AgentSandboxFactory:
    """Factory for creating AgentSandboxBackend instances.

    Usage:
        backend = AgentSandboxFactory.create("docker", httpx_client)
    """

    _backends = {
        "docker": DockerAgentSandboxBackend,
    }

    @classmethod
    def create(
        cls,
        sandbox_type: str,
    ) -> AgentSandboxBackend:
        """Create a sandbox backend for the given type.

        Args:
            sandbox_type: Type of sandbox (docker/e2b/opensandbox)

        Returns:
            AgentSandboxBackend instance

        Raises:
            ValueError: If sandbox type is not supported
        """
        if sandbox_type not in cls._backends:
            raise ValueError(
                f"Unsupported sandbox type: {sandbox_type}. "
                f"Supported types: {list(cls._backends.keys())}"
            )

        backend_class = cls._backends[sandbox_type]
        return backend_class()

    @classmethod
    def register(cls, sandbox_type: str, backend_class: type[AgentSandboxBackend]) -> None:
        """Register a new sandbox backend type.

        Args:
            sandbox_type: Type identifier
            backend_class: Backend class
        """
        cls._backends[sandbox_type] = backend_class
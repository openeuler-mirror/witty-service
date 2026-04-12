from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.domain.errors import DomainError

SANDBOX_NOT_FOUND = "SANDBOX_NOT_FOUND"
SANDBOX_START_FAILED = "SANDBOX_START_FAILED"
SANDBOX_STOP_FAILED = "SANDBOX_STOP_FAILED"
SANDBOX_NOT_SUPPORTED = "SANDBOX_NOT_SUPPORTED"


class SandboxStatus(str, Enum):
    starting = "starting"
    running = "running"
    stopped = "stopped"
    error = "error"


@dataclass(slots=True, frozen=True)
class SandboxHandle:
    sandbox_id: str
    agent_id: str
    workspace_path: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class AdapterEndpoint:
    base_url: str
    health_url: str | None = None

    @property
    def ws_url(self) -> str:
        if self.base_url.startswith("https"):
            scheme = "wss"
        else:
            scheme = "ws"
        host = self.base_url.split("://")[-1]
        return f"{scheme}://{host}/agent/sessions/{{session_id}}/ws"

    def ws_endpoint(self, session_id: str) -> str:
        if self.base_url.startswith("https"):
            scheme = "wss"
        else:
            scheme = "ws"
        host = self.base_url.split("://")[-1]
        return f"{scheme}://{host}/agent/sessions/{session_id}/ws"


class SandboxBackend(ABC):
    sandbox_type: str = "unknown"

    @abstractmethod
    def start(
        self,
        *,
        agent_id: str,
        workspace_path: str,
        **kwargs: Any,
    ) -> SandboxHandle:
        raise NotImplementedError

    @abstractmethod
    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        raise NotImplementedError

    @abstractmethod
    def status(self, handle: SandboxHandle | str, **kwargs: Any) -> SandboxStatus:
        raise NotImplementedError

    @abstractmethod
    def endpoint(
        self, handle: SandboxHandle | str, **kwargs: Any
    ) -> AdapterEndpoint:
        raise NotImplementedError

    @abstractmethod
    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        raise NotImplementedError


def sandbox_not_supported(*, sandbox_type: str, operation: str) -> DomainError:
    return DomainError(
        code=SANDBOX_NOT_SUPPORTED,
        message="Sandbox backend is not supported yet.",
        details={
            "sandbox_type": sandbox_type,
            "operation": operation,
        },
    )


def sandbox_not_found(*, sandbox_type: str, sandbox_id: str) -> DomainError:
    return DomainError(
        code=SANDBOX_NOT_FOUND,
        message="Sandbox handle was not found.",
        details={
            "sandbox_type": sandbox_type,
            "sandbox_id": sandbox_id,
        },
    )


def sandbox_start_failed(
    *,
    sandbox_type: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> DomainError:
    payload = {
        "sandbox_type": sandbox_type,
    }
    payload.update(details or {})
    return DomainError(
        code=SANDBOX_START_FAILED,
        message=message,
        details=payload,
    )


def sandbox_stop_failed(
    *,
    sandbox_type: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> DomainError:
    payload = {
        "sandbox_type": sandbox_type,
    }
    payload.update(details or {})
    return DomainError(
        code=SANDBOX_STOP_FAILED,
        message=message,
        details=payload,
    )

from __future__ import annotations

from typing import Any, NoReturn

from src.sandbox.base import (
    AdapterEndpoint,
    SandboxBackend,
    SandboxHandle,
    SandboxStatus,
    sandbox_not_supported,
)


class E2BSandboxBackend(SandboxBackend):
    sandbox_type = "e2b"

    def _raise_not_supported(self, operation: str) -> NoReturn:
        raise sandbox_not_supported(sandbox_type=self.sandbox_type, operation=operation)

    def start(
        self,
        *,
        agent_id: str,
        workspace_path: str,
        **kwargs: Any,
    ) -> SandboxHandle:
        self._raise_not_supported("start")

    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        self._raise_not_supported("stop")

    def status(self, handle: SandboxHandle | str, **kwargs: Any) -> SandboxStatus:
        self._raise_not_supported("status")

    def endpoint(
        self, handle: SandboxHandle | str, **kwargs: Any
    ) -> AdapterEndpoint:
        self._raise_not_supported("endpoint")

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        self._raise_not_supported("cleanup")

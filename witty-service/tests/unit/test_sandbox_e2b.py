from __future__ import annotations

import pytest

from src.domain.errors import DomainError
from src.sandbox.base import SandboxBackend
from src.sandbox.e2b import E2BSandboxBackend
from src.sandbox.factory import create_sandbox_backend, register_sandbox_backend


@pytest.mark.parametrize(
    "operation,args,kwargs",
    [
        ("start", (), {"agent_id": "agent-1", "workspace_path": "/tmp/workspace"}),
        ("stop", ("sandbox-handle",), {}),
        ("status", ("sandbox-handle",), {}),
        ("endpoint", ("sandbox-handle",), {}),
    ],
)
def test_e2b_backend_raises_sandbox_not_supported(operation, args, kwargs):
    backend = E2BSandboxBackend()

    with pytest.raises(DomainError) as exc_info:
        getattr(backend, operation)(*args, **kwargs)

    assert exc_info.value.code == "SANDBOX_NOT_SUPPORTED"
    assert exc_info.value.message == "Sandbox backend is not supported yet."


def test_sandbox_backend_abstract_methods_are_minimal():
    assert SandboxBackend.__abstractmethods__ == {
        "start",
        "stop",
        "status",
        "endpoint",
        "cleanup",
    }


def test_sandbox_factory_returns_e2b_backend():
    backend = create_sandbox_backend("e2b")

    assert isinstance(backend, E2BSandboxBackend)


def test_sandbox_factory_is_case_insensitive():
    backend = create_sandbox_backend("E2B")

    assert isinstance(backend, E2BSandboxBackend)


def test_sandbox_factory_raises_sandbox_not_supported_for_unknown_sandbox():
    with pytest.raises(DomainError) as exc_info:
        create_sandbox_backend("unknown-sandbox")

    assert exc_info.value.code == "SANDBOX_NOT_SUPPORTED"
    assert exc_info.value.details == {
        "sandbox_type": "unknown-sandbox",
        "operation": "create",
    }


def test_sandbox_factory_uses_registered_backend():
    class DummySandboxBackend(SandboxBackend):
        sandbox_type = "dummy"

        def start(self, *, agent_id: str, workspace_path: str, **kwargs):
            raise AssertionError("not called")

        def stop(self, handle, **kwargs):
            raise AssertionError("not called")

        def status(self, handle, **kwargs):
            raise AssertionError("not called")

        def endpoint(self, handle, **kwargs):
            raise AssertionError("not called")

        def cleanup(self, handle, **kwargs):
            raise AssertionError("not called")

    register_sandbox_backend("dummy", DummySandboxBackend)

    backend = create_sandbox_backend("dummy")

    assert isinstance(backend, DummySandboxBackend)

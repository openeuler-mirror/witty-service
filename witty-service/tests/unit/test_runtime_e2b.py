from __future__ import annotations

import pytest

from witty_service.domain.errors import DomainError
from witty_service.runtime.base import RuntimeBackend
from witty_service.runtime.e2b import E2BRuntimeBackend
from witty_service.runtime.factory import create_runtime_backend, register_runtime_backend


@pytest.mark.parametrize(
    "operation,args,kwargs",
    [
        ("start", (), {"agent_id": "agent-1", "workspace_path": "/tmp/workspace"}),
        ("stop", ("runtime-handle",), {}),
        ("status", ("runtime-handle",), {}),
        ("endpoint", ("runtime-handle",), {}),
    ],
)
def test_e2b_backend_raises_runtime_not_supported(operation, args, kwargs):
    backend = E2BRuntimeBackend()

    with pytest.raises(DomainError) as exc_info:
        getattr(backend, operation)(*args, **kwargs)

    assert exc_info.value.code == "RUNTIME_NOT_SUPPORTED"
    assert exc_info.value.message == "Runtime backend is not supported yet."


def test_runtime_backend_abstract_methods_are_minimal():
    assert RuntimeBackend.__abstractmethods__ == {
        "start",
        "stop",
        "status",
        "endpoint",
        "cleanup",
    }


def test_runtime_factory_returns_e2b_backend():
    backend = create_runtime_backend("e2b")

    assert isinstance(backend, E2BRuntimeBackend)


def test_runtime_factory_is_case_insensitive():
    backend = create_runtime_backend("E2B")

    assert isinstance(backend, E2BRuntimeBackend)


def test_runtime_factory_raises_runtime_not_supported_for_unknown_runtime():
    with pytest.raises(DomainError) as exc_info:
        create_runtime_backend("unknown-runtime")

    assert exc_info.value.code == "RUNTIME_NOT_SUPPORTED"
    assert exc_info.value.details == {
        "runtime_type": "unknown-runtime",
        "operation": "create",
    }


def test_runtime_factory_uses_registered_backend():
    class DummyRuntimeBackend(RuntimeBackend):
        runtime_type = "dummy"

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

    register_runtime_backend("dummy", DummyRuntimeBackend)

    backend = create_runtime_backend("dummy")

    assert isinstance(backend, DummyRuntimeBackend)

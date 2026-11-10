from __future__ import annotations

from typing import Any

from fastapi import FastAPI

_HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


def _status_key(status: str) -> tuple[int, str]:
    return (int(status), "") if status.isdigit() else (1000, status)


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _finalize(spec: dict[str, Any]) -> dict[str, Any]:
    for path, path_item in spec.get("paths", {}).items():
        if not _is_api_path(path):
            continue
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if responses is None:
                continue
            responses.pop("422", None)
            operation["responses"] = dict(
                sorted(responses.items(), key=lambda item: _status_key(item[0]))
            )

    has_422 = any(
        "422" in operation.get("responses", {})
        for path_item in spec.get("paths", {}).values()
        for method, operation in path_item.items()
        if method in _HTTP_METHODS and isinstance(operation, dict)
    )
    if not has_422:
        schemas = spec.get("components", {}).get("schemas", {})
        for name in ("HTTPValidationError", "ValidationError"):
            schemas.pop(name, None)
    return spec


class ApiApp(FastAPI):
    """`/api` 操作按契约返回 400；旧根路径入口保留 FastAPI 默认的 422。"""

    def openapi(self) -> dict[str, Any]:
        return _finalize(super().openapi())

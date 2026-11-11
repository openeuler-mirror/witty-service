from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse

from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    """details 只含非密信息：字段校验为 `errors`（每项含 `field`、`type`、`reason`；
    `field` 为空表示整份请求体），状态冲突可含 `active_turn_id`、`allowed_actions`、
    `current_status`，可用性问题可含 `retry_after`。
    """

    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "SESSION_NOT_FOUND",
                        "message": "Session was not found.",
                        "details": {"session_id": "ses_01"},
                    }
                }
            ]
        }
    )

    error: ErrorDetail


_ERROR_DESCRIPTIONS: Final[dict[int, str]] = {
    400: "参数错误：请求体、查询或路径参数不合法，优先级最高",
    401: "缺少或错误的 Bearer token",
    404: "目标资源不存在",
    405: "HTTP 方法不被该入口支持",
    409: "当前状态不允许该操作",
    500: "未预期的服务端错误",
    501: "当前里程碑未实现，或探针失败后明确降级的能力",
    503: "Runtime 或配置发布暂不可用，可稍后重试",
}

_LOCATION_PREFIXES: Final[frozenset[str]] = frozenset(
    {"body", "query", "path", "header", "cookie"}
)

_HTTP_ERROR_CODES: Final[dict[int, ErrorCode]] = {
    401: ErrorCode.UNAUTHORIZED,
    404: ErrorCode.ENDPOINT_NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
}

ERROR_STATUS: Final[dict[ErrorCode, int]] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.INVALID_WORKSPACE_PATH: 400,
    ErrorCode.MODEL_REQUIRED: 400,
    ErrorCode.INVALID_CURSOR: 400,
    ErrorCode.CURSOR_SESSION_MISMATCH: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.WORKSPACE_NOT_FOUND: 404,
    ErrorCode.SESSION_NOT_FOUND: 404,
    ErrorCode.TURN_NOT_FOUND: 404,
    ErrorCode.AGENT_NOT_FOUND: 404,
    ErrorCode.MODEL_NOT_FOUND: 404,
    ErrorCode.ENDPOINT_NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.ACTIVE_TURN_CONFLICT: 409,
    ErrorCode.SESSION_ADMISSION_CLOSED: 409,
    ErrorCode.WORKSPACE_DIRECTORY_CONFLICT: 409,
    ErrorCode.AGENT_NAME_CONFLICT: 409,
    ErrorCode.CONFIG_UPDATE_CONFLICT: 409,
    ErrorCode.CONFIG_PROJECTION_INVALID: 409,
    ErrorCode.PRIMARY_AGENT_IMMUTABLE: 409,
    ErrorCode.AGENT_ARCHIVED: 409,
    ErrorCode.MODEL_DISABLED: 409,
    ErrorCode.MODEL_PROVIDER_CONFLICT: 409,
    ErrorCode.MODEL_CREDENTIAL_CONFLICT: 409,
    ErrorCode.MODEL_CREDENTIAL_REQUIRED: 400,
    ErrorCode.MODEL_CREDENTIAL_UNAVAILABLE: 503,
    ErrorCode.MODEL_CREDENTIAL_INVALID: 503,
    ErrorCode.MODEL_IN_USE: 409,
    ErrorCode.INVALID_TURN_OPERATION: 409,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.ENDPOINT_NOT_IMPLEMENTED: 501,
    ErrorCode.RUNTIME_UNAVAILABLE: 503,
    ErrorCode.CONFIG_PUBLISH_UNAVAILABLE: 503,
}

#: 同一请求命中多类错误时依次返回第一个适用错误；401 与 501 不属于业务优先级。
ERROR_PRIORITY: Final[tuple[int, ...]] = (400, 404, 409, 503)


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {}
    for status in statuses:
        response: dict[str, Any] = {
            "model": ErrorEnvelope,
            "description": _ERROR_DESCRIPTIONS[status],
        }
        if status == 401:
            response["headers"] = {
                "WWW-Authenticate": {
                    "description": "Bearer challenge",
                    "schema": {"type": "string"},
                }
            }
        responses[status] = response
    return responses


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": str(code),
                "message": message,
                "details": dict(details or {}),
            }
        },
        headers=headers,
    )


def _field_name(location: tuple[Any, ...]) -> str:
    parts = [str(part) for part in location]
    if parts and parts[0] in _LOCATION_PREFIXES:
        parts = parts[1:]
    return ".".join(parts)


def _validation_details(exc: RequestValidationError) -> dict[str, Any]:
    errors = []
    for error in exc.errors():
        reason = str(error.get("msg", "invalid value"))
        errors.append(
            {
                "field": _field_name(tuple(error.get("loc", ()))),
                "type": str(error.get("type", "value_error")),
                "reason": reason.removeprefix("Value error, "),
            }
        )
    return {"errors": errors}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        payload = exc.to_payload()
        headers = (
            {"WWW-Authenticate": "Bearer"}
            if exc.code == ErrorCode.UNAUTHORIZED
            else None
        )
        return error_response(
            status_code=exc.status_code,
            code=payload.code,
            message=payload.message,
            details=payload.details,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> Any:
        if not _is_api_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        return error_response(
            status_code=400,
            code=ErrorCode.VALIDATION_ERROR,
            message="request validation failed",
            details=_validation_details(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> Any:
        if not _is_api_path(request.url.path):
            return await http_exception_handler(request, exc)
        return error_response(
            status_code=exc.status_code,
            code=_HTTP_ERROR_CODES.get(exc.status_code, ErrorCode.INTERNAL_ERROR),
            message=str(exc.detail),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> Any:
        if not _is_api_path(request.url.path):
            return PlainTextResponse("Internal Server Error", status_code=500)
        logger.exception(
            "Unhandled error on %s %s", request.method, request.url.path, exc_info=exc
        )
        return error_response(
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="internal server error",
        )

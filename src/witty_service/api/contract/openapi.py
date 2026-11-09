from __future__ import annotations

from typing import Any

import yaml
from fastapi import FastAPI

from witty_service.api.contract.routers import router
from witty_service.api.errors import (
    ERROR_PRIORITY,
    ERROR_STATUS,
    register_exception_handlers,
)
from witty_service.api.openapi import ApiApp

CONTRACT_TITLE = "Witty Service API"
CONTRACT_VERSION = "0.1.0"

SERVER_URL = "http://127.0.0.1:8000"


def _error_table() -> str:
    rows = "\n".join(
        f"| {status} | `{code.value}` |" for code, status in ERROR_STATUS.items()
    )
    return f"| 状态 | 错误码 |\n| --- | --- |\n{rows}"


def _description() -> str:
    priority = " → ".join(str(status) for status in ERROR_PRIORITY)
    return f"""\
Witty Service 本地控制面 API 契约（C03 Draft）。

本文件由 `python scripts/render_openapi.py` 从 `src/witty_service/api/contract/`
生成，不要手工编辑。

## 范围

覆盖 Workspace、Session、Turn、Message、Agent 与事件流。Console、Channel、Scheduler
共用 Session/Turn 接口，外部入口不绕过回合准入。

## 通用约定

- `/api` 下错误响应统一为 `{{"error": {{"code", "message", "details"}}}}`，details 只包含非密信息。
- `/api` 下请求体、查询与路径参数校验失败返回 400 `VALIDATION_ERROR`，不使用 422；
  `details.errors` 每项含 `field`、`type`、`reason`，其中 `type` 供客户端分支，
  `reason` 仅供人类阅读；旧根路径入口在 U03/U09 前保持现有行为。
- 同一请求命中多类错误时，判定优先级固定为 {priority}；401 认证失败先于业务错误。
  `MODEL_REQUIRED` 是 Turn 准入的业务前置条件，不是请求格式错误：先解析路径中的
  Session（不存在返回 404 `SESSION_NOT_FOUND`），再检查当前模型（为空返回 400）。
- 不属于路径中 Session 的 Turn 按 404 `TURN_NOT_FOUND` 返回，不泄露其是否存在。
- abort 幂等：重复调用返回 200 且 `already_stopped=true`。
- 事件流每帧固定为 `data: <SessionEvent JSON>`，不使用 SSE 的 `id:`/`event:`/
  `retry:`，不支持 `Last-Event-ID`；断线后按 `resync_required` 全量重读。客户端用
  fetch + Bearer 读取，不支持原生 `EventSource`。P0 不提供 HITL，不产生
  `question.*` 事件。
- 没有显式配置发布接口：保存配置不停服，下一 Turn 自动确认最新投影已发布；
  等待超过 10 秒返回 503，且不插入 running Turn。
- Workspace 的 `host_directory` 只出现在创建请求中，任何响应都不回传宿主路径。
- 本契约仍为 Draft：尚未实现的操作返回 501 `ENDPOINT_NOT_IMPLEMENTED`，
  经真实入口验证后才标 Verified Frozen。

## 实现状态

接口按里程碑逐段实现，验收前均保持 Draft：M2 为 Session、Turn、Message 与事件流；
M3 为 Agent 编辑/归档与 Session MCP 激活；M4 为 Workspace CRUD、abort、重生成与
Session 删除。

## 错误码

{_error_table()}
"""


def build_contract_app() -> FastAPI:
    app = ApiApp(
        title=CONTRACT_TITLE,
        version=CONTRACT_VERSION,
        description=_description(),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    register_exception_handlers(app)
    app.include_router(router)
    return app


class _ContractDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ContractDumper.add_representer(str, _represent_str)


def render_contract_openapi() -> dict[str, Any]:
    spec = build_contract_app().openapi()
    spec["servers"] = [
        {
            "url": SERVER_URL,
            "description": "本机服务默认监听地址；实际端口以启动配置为准",
        }
    ]
    return spec


def render_contract_openapi_yaml() -> str:
    return yaml.dump(
        render_contract_openapi(),
        Dumper=_ContractDumper,
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AppContainer",
    "AgentBinding",
    "BindingResolutionRequest",
    "RuntimeInstance",
    "RuntimeContextConfig",
    "RuntimeInstanceManager",
    "RuntimeTarget",
]


def __getattr__(name: str) -> Any:
    """按需加载 composition 导出，避免包导入时触发容器级循环依赖。"""
    if name == "AppContainer":
        return import_module(
            "witty_agent_server.application.composition.app_container"
        ).AppContainer
    if name == "RuntimeInstanceManager":
        return import_module(
            "witty_agent_server.application.composition.runtime_instance_manager"
        ).RuntimeInstanceManager
    if name in {
        "AgentBinding",
        "BindingResolutionRequest",
        "RuntimeInstance",
        "RuntimeContextConfig",
        "RuntimeTarget",
    }:
        module = import_module("witty_agent_server.application.composition.models")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

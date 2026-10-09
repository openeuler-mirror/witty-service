"""MCP server 配置 → opencode ``opencode.json`` ``mcp`` 条目的纯转换器。

供两条路径复用，保证产出形状一致：

- agent-server ``POST /agent/mcp/enable``（``opencode_lifecycle_service``）；
- witty-service 模板实例化 ``_merge_opencode_mcp``（``agent_template_service``）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def to_opencode_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    """将 MCP 配置 dict 转换为 opencode serve 可接受的格式。

    转换后输出:
        {"type": "local", "command": ["npx", "-y", "..."], "environment": {...}, "enabled": true}
    """
    # 推断 opencode type
    oc_type: str = "remote" if "url" in config else "local"
    oc_config: dict[str, Any] = {"type": oc_type}

    if oc_type == "local":
        command = config.get("command")
        args = config.get("args")
        if isinstance(command, str):
            merged = [command]
            if isinstance(args, list):
                merged.extend(str(a) for a in args)
            oc_config["command"] = merged
        elif isinstance(command, list):
            merged = list(command)
            if isinstance(args, list):
                merged.extend(str(a) for a in args)
            oc_config["command"] = merged
        else:
            logger.warning(
                "MCP server config has invalid 'command' field (type=%s), "
                "server will not be functional. config keys: %s",
                type(command).__name__,
                list(config.keys()),
            )
            oc_config["enabled"] = False
            return oc_config
        if "env" in config:
            oc_config["environment"] = config["env"]
        if "cwd" in config:
            oc_config["cwd"] = config["cwd"]
    else:
        # remote
        if "url" in config:
            oc_config["url"] = config["url"]
        if "headers" in config:
            oc_config["headers"] = config["headers"]

    oc_config["enabled"] = config.get("enabled", True)

    return oc_config

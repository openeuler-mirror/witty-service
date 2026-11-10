"""最小 stdio MCP server，供探针验证工具名 sanitize 与 ``tools`` 过滤。

只实现 MCP 的 stdio 传输和三个方法：``initialize``、``tools/list``、
``tools/call``。工具名刻意包含大小写、下划线和连字符，用来观察 OpenCode
把它们暴露给 provider 时如何改名。
"""

from __future__ import annotations

import json
import sys
from typing import Any

_PROTOCOL_VERSION = "2024-11-05"

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo_upper",
        "description": "Return the given text in upper case",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "echo_lower",
        "description": "Return the given text in lower case",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "slow.echo",
        "description": "Return the given text after a short delay",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
]


def _text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _handle(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if method == "initialize":
        return {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "probe-echo", "version": "0.1.0"},
        }
    if method == "tools/list":
        return {"tools": _TOOLS}
    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        text = str(arguments.get("text", ""))
        if name == "echo_upper":
            return _text_result(text.upper())
        if name == "echo_lower":
            return _text_result(text.lower())
        if name == "slow.echo":
            return _text_result(f"slow:{text}")
        return {
            "content": [{"type": "text", "text": f"unknown tool {name}"}],
            "isError": True,
        }
    if method == "ping":
        return {}
    return None


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        message_id = message.get("id")
        if message_id is None:
            continue  # 通知不需要响应
        result = _handle(method, message.get("params") or {})
        if result is None:
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        else:
            payload = {"jsonrpc": "2.0", "id": message_id, "result": result}
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

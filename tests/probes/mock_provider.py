"""OpenAI 兼容的假 provider，用于观察 OpenCode 真实发往模型的请求。

探针关心的不是模型能力，而是 **provider 实际收到了什么**：``tools`` 数组里的
工具名、system prompt 内容、历史消息条数和 ``model`` 字段。因此这里用本地
HTTP 服务顶替真实 provider，把每个请求体原样记录下来供断言使用。

脚本化行为由“本轮最后一条 user 文本”触发：

``CALLTOOL <provider_tool_name>``
    回复一个 tool_call。``bash`` 取后续文本作为命令，``skill`` 固定调用
    ``probe-skill``，其余 MCP 工具传 ``{"text": "probe"}``。
``SLOW``
    在**同一条 SSE 流**里分多块、带延迟地返回 ``PROBE_OK``。
``ERROR500``
    返回 HTTP 500，用于验证 provider 失败时的有界重试。
``TIMEOUT``
    延迟后直接断开连接（不写响应），模拟 transport 超时/断连。

协议为 OpenAI ``/v1/chat/completions`` 流式（SSE + ``Transfer-Encoding:
chunked``），适配 ``@ai-sdk/openai-compatible``。所有请求按 marker 锚定，
断言不依赖请求顺序。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _chunk(
    *, delta: dict[str, Any], finish: str | None, model: str = "mock-model"
) -> bytes:
    payload = {
        "id": "chatcmpl-probe",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def _text_chunks(text: str, *, delay: float = 0.0) -> list[tuple[bytes, float]]:
    """把文本拆成多块 SSE，确保 delta 是逐块而不是整段。"""
    split = max(1, len(text) // 2)
    return [
        (_chunk(delta={"role": "assistant"}, finish=None), 0.0),
        (_chunk(delta={"content": text[:split]}, finish=None), 0.0),
        (_chunk(delta={"content": text[split:]}, finish=None), delay),
        (_chunk(delta={}, finish="stop"), 0.0),
    ]


def _abc_chunks() -> list[tuple[bytes, float]]:
    """逐字符、带间隔地返回 ``ABC``，用于验证流式 delta 是否逐块到达。"""
    return [
        (_chunk(delta={"role": "assistant"}, finish=None), 0.0),
        (_chunk(delta={"content": "A"}, finish=None), 0.1),
        (_chunk(delta={"content": "B"}, finish=None), 0.1),
        (_chunk(delta={"content": "C"}, finish=None), 0.1),
        (_chunk(delta={}, finish="stop"), 0.0),
    ]


def _tool_call_chunks(tool_name: str, arguments: str) -> list[tuple[bytes, float]]:
    call = {
        "index": 0,
        "id": "call_probe_1",
        "type": "function",
        "function": {"name": tool_name, "arguments": arguments},
    }
    return [
        (_chunk(delta={"role": "assistant"}, finish=None), 0.0),
        (_chunk(delta={"tool_calls": [call]}, finish=None), 0.0),
        (_chunk(delta={}, finish="tool_calls"), 0.0),
    ]


_DONE = b"data: [DONE]\n\n"


def _content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def turn_user_text(body: dict[str, Any]) -> str:
    """返回本轮最后一条 user 消息的文本。

    工具续轮的最后一条消息是 ``tool``，但倒数第一条 user 仍是本轮 prompt；
    用“最后一条 user”而不是“包含 marker 的任意消息”，可以避免后续 Turn
    的历史把前一轮 marker 再次匹配进来。
    """
    messages = body.get("messages") or []
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_text(message)
    return ""


def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    return any(message.get("role") in {"tool", "tool_result"} for message in messages)


class MockProvider:
    """线程内运行的 OpenAI 兼容 provider，记录收到的每个请求体。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("mock provider is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def _body(self) -> bytes:
                length = int(self.headers.get("content-length", "0"))
                return self.rfile.read(length) if length else b"{}"

            def do_POST(self) -> None:
                raw = self._body()
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                if not self.path.endswith("/chat/completions"):
                    self.send_error(404)
                    return
                with provider._lock:
                    provider.requests.append(body)
                user_text = turn_user_text(body)
                if "ERROR500" in user_text:
                    self._send_error_500()
                    return
                if "TIMEOUT" in user_text:
                    time.sleep(1.0)
                    # 不写任何响应直接断开，让客户端看到 transport 错误。
                    self.close_connection = True
                    return
                chunks = provider._chunks(body, user_text)
                self._send_chunked(chunks)

            def _send_error_500(self) -> None:
                payload = json.dumps(
                    {
                        "error": {
                            "message": "mock provider forced 500",
                            "type": "server_error",
                        }
                    }
                ).encode()
                try:
                    self.send_response(500)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

            def _send_chunked(self, chunks: Iterable[tuple[bytes, float]]) -> None:
                try:
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("cache-control", "no-cache")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for chunk, delay in chunks:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode())
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                        if delay:
                            time.sleep(delay)
                    self.wfile.write(f"{len(_DONE):X}\r\n".encode())
                    self.wfile.write(_DONE)
                    self.wfile.write(b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # 客户端 abort 时会主动断开连接，这是被测行为本身。
                    return

            def do_GET(self) -> None:
                if self.path.endswith("/models"):
                    payload = json.dumps(
                        {
                            "object": "list",
                            "data": [{"id": "mock-model", "object": "model"}],
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_error(404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def _chunks(
        self, body: dict[str, Any], user_text: str
    ) -> list[tuple[bytes, float]]:
        messages = body.get("messages") or []
        if "CALLTOOL " in user_text and not _has_tool_result(messages):
            rest = user_text.split("CALLTOOL ", 1)[1].strip()
            tool_name, _, tail = rest.partition(" ")
            if tool_name == "skill":
                arguments = json.dumps({"name": "probe-skill"})
            elif tool_name == "bash":
                arguments = json.dumps(
                    {"command": tail.strip() or "pwd", "description": "probe pwd"}
                )
            else:
                arguments = json.dumps({"text": "probe"})
            return _tool_call_chunks(tool_name, arguments)
        if "STREAM_ABC" in user_text:
            return _abc_chunks()
        if "SLOW" in user_text:
            return _text_chunks("PROBE_OK", delay=0.6)
        return _text_chunks("PROBE_OK")

    # ----------------------------------------------------------------- helpers
    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.requests)

    def requests_for(self, marker: str) -> list[dict[str, Any]]:
        """本轮（最后一条 user 文本含 marker）产生的全部请求。"""
        return [body for body in self.snapshot() if marker in turn_user_text(body)]

    def request_for(self, marker: str) -> dict[str, Any]:
        matched = self.requests_for(marker)
        if not matched:
            raise AssertionError(f"mock provider 没收到 marker={marker!r} 的请求")
        return matched[0]

    def tool_names_for(self, marker: str) -> list[str]:
        body = self.request_for(marker)
        tools = body.get("tools") or []
        return [
            str(tool.get("function", {}).get("name", ""))
            for tool in tools
            if isinstance(tool, dict)
        ]

    def tool_results_for(self, marker: str) -> list[str]:
        """本轮所有 tool 结果消息的文本，用于断言工具往返。"""
        results: list[str] = []
        for body in self.requests_for(marker):
            for message in body.get("messages") or []:
                if message.get("role") in {"tool", "tool_result"}:
                    results.append(_content_text(message))
        return results

    def count_for(self, marker: str) -> int:
        return len(self.requests_for(marker))

    def wait_for_marker(
        self, marker: str, count: int = 1, timeout: float = 30.0
    ) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.count_for(marker) >= count:
                return
            time.sleep(0.05)
        raise AssertionError(
            f"mock provider 在 {timeout}s 内只收到 {self.count_for(marker)} 次 "
            f"marker={marker!r} 的请求，期望至少 {count} 次"
        )

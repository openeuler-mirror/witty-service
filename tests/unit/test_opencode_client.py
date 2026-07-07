from __future__ import annotations

from typing import Any

import httpx
import pytest

from witty_agent_server.infra.clients.opencode_client import (
    OpenCodeClient,
    OpenCodeClientError,
)


def _client_with_transport(
    handler, *, serve_port: int = 4096
) -> OpenCodeClient:
    """构造一个 OpenCodeClient，并把长持有 httpx.Client 直接塞进去。

    `OpenCodeClient.http_client()` 现在返回长持有实例，因此测试不再 monkeypatch
    方法，而是直接构造带 MockTransport 的 httpx.Client 注入到 `_http_client`。
    """
    client = OpenCodeClient(serve_port=serve_port, username="u", password="p")
    client._http_client = httpx.Client(
        base_url=client.server_url,
        auth=("u", "p"),
        transport=httpx.MockTransport(handler),
        timeout=30.0,
    )
    return client


def test_list_agents_returns_default_and_agents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/agent"
        return httpx.Response(
            200,
            json=[{"id": "main", "default": True}, {"id": "aux"}],
        )

    client = _client_with_transport(handler)

    result = client.list_agents()

    assert result["defaultId"] == "main"
    assert [a["id"] for a in result["agents"]] == ["main", "aux"]


def test_list_agents_empty_list_defaults_main() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = _client_with_transport(handler)

    assert client.list_agents() == {"defaultId": "main", "agents": []}


def test_list_sessions_ignores_agent_id() -> None:
    received: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.url.path)
        return httpx.Response(200, json=[{"id": "s1"}])

    client = _client_with_transport(handler)

    result = client.list_sessions(agent_id="ignored")

    assert result == {"sessions": [{"id": "s1"}]}
    assert received == ["/session"]


def test_create_session_stores_session_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session"
        body = request.read().decode()
        assert '"title":"my-key"' in body.replace(" ", "")
        return httpx.Response(200, json={"id": "opencode-sess-1"})

    client = _client_with_transport(handler)

    client.create_session(session_key="my-key")

    assert client.resolve_session_id("my-key") == "opencode-sess-1"


def test_delete_session_uses_mapped_id() -> None:
    deleted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "real-id"})
        deleted_paths.append(request.url.path)
        return httpx.Response(204)

    client = _client_with_transport(handler)
    client.create_session(session_key="k")
    client.delete_session(session_key="k")

    assert deleted_paths == ["/session/real-id"]
    assert client.resolve_session_id("k") is None


@pytest.mark.parametrize(
    "op",
    ["delete_session", "abort_session"],
)
def test_session_op_without_mapping_raises_409(op: str) -> None:
    """_session_map 未命中时（无预创建 mapping / 历史 session）抛 409。"""
    client = _client_with_transport(lambda r: httpx.Response(204))

    with pytest.raises(OpenCodeClientError) as exc:
        getattr(client, op)(session_key="raw-key")

    assert exc.value.status == 409
    assert "raw-key" in exc.value.message


def test_abort_session_uses_mapped_id() -> None:
    aborted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "abort-real-id"})
        aborted_paths.append(request.url.path)
        return httpx.Response(204)

    client = _client_with_transport(handler)
    client.create_session(session_key="k")
    client.abort_session(session_key="k")

    assert aborted_paths == ["/session/abort-real-id/abort"]


# ---------------------------------------------------------------------------
# _session_map fallback：重启/多 worker 后通过 list 查 title 找回 opencode id
# ---------------------------------------------------------------------------


def test_delete_session_falls_back_to_list_lookup_by_title() -> None:
    """_session_map miss 时按 title 在 /session 列表里找回 id，再 DELETE。"""
    deleted_paths: list[str] = []
    list_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/session":
            list_seen.append(request.url.path)
            return httpx.Response(
                200,
                json=[{"id": "oc-123", "title": "recovered-key"}],
            )
        if request.method == "DELETE":
            deleted_paths.append(request.url.path)
        return httpx.Response(204)

    client = _client_with_transport(handler)

    # 不预填 _session_map，直接 delete -> 触发 list fallback
    client.delete_session(session_key="recovered-key")

    assert list_seen == ["/session"]
    assert deleted_paths == ["/session/oc-123"]
    # 成功删除后从 mapping 清掉（与 delete_session 既有契约一致）
    assert client.resolve_session_id("recovered-key") is None


def test_delete_session_list_without_title_falls_back_to_409() -> None:
    """list 响应不含 title 字段（schema 不一致）时回退到 409 而非误删。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/session":
            return httpx.Response(200, json=[{"id": "x"}])  # 无 title
        return httpx.Response(204)

    client = _client_with_transport(handler)

    with pytest.raises(OpenCodeClientError) as exc:
        client.delete_session(session_key="raw-key")

    assert exc.value.status == 409
    assert "raw-key" in exc.value.message


def test_delete_session_list_empty_falls_back_to_409() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/session":
            return httpx.Response(200, json=[])
        return httpx.Response(204)

    client = _client_with_transport(handler)

    with pytest.raises(OpenCodeClientError) as exc:
        client.delete_session(session_key="raw-key")

    assert exc.value.status == 409


def test_abort_session_falls_back_to_list_lookup_by_title() -> None:
    aborted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/session":
            return httpx.Response(200, json=[{"id": "oc-456", "title": "k-abort"}])
        if request.method == "POST" and request.url.path.endswith("/abort"):
            aborted_paths.append(request.url.path)
        return httpx.Response(204)

    client = _client_with_transport(handler)

    client.abort_session(session_key="k-abort")

    assert aborted_paths == ["/session/oc-456/abort"]
    assert client.resolve_session_id("k-abort") == "oc-456"


def test_delete_session_uses_mapped_id_preferentially() -> None:
    """mapping 已存在时不触发 /session 列表查询，优先走内存映射。"""
    list_invoked = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_invoked
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "mapped-id"})
        if request.method == "GET" and request.url.path == "/session":
            list_invoked = True
            return httpx.Response(200, json=[])
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(204)

    client = _client_with_transport(handler)
    client.create_session(session_key="k")
    client.delete_session(session_key="k")

    assert not list_invoked
    assert client.resolve_session_id("k") is None


# ---------------------------------------------------------------------------
# stream_client() 工厂：M4 SSE 用，独立 client，可安全 with
# ---------------------------------------------------------------------------


def test_stream_client_returns_new_instance_each_call() -> None:
    client = OpenCodeClient(serve_port=4096, username="u", password="p")

    c1 = client.stream_client()
    c2 = client.stream_client()

    assert c1 is not c2
    assert not c1.is_closed
    assert not c2.is_closed
    c1.close()
    c2.close()
    client.close()


def test_stream_client_does_not_affect_rpc_client() -> None:
    """stream_client() 不缓存、不替换 RPC 长持有 client。"""
    client = OpenCodeClient(serve_port=4096, username="u", password="p")
    rpc1 = client.http_client()

    sc = client.stream_client()
    sc.close()

    rpc2 = client.http_client()
    assert rpc1 is rpc2  # RPC client 长持有、不受 stream 影响
    assert not rpc2.is_closed
    client.close()


def test_stream_client_close_safety() -> None:
    """stream_client() 用 with 关闭后，RPC 调用仍可继续（验证独立生命周期）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "main"}])

    client = OpenCodeClient(serve_port=4096, username="u", password="p")
    client._http_client = httpx.Client(
        base_url=client.server_url,
        auth=("u", "p"),
        transport=httpx.MockTransport(handler),
        timeout=30.0,
    )

    # 用 with 关闭 stream client，不应影响 RPC client
    with client.stream_client() as sc:
        assert sc is not client.http_client()

    result = client.list_agents()  # RPC 走长持有 http_client()
    assert result["defaultId"] == "main"
    client.close()


def test_http_error_raises_client_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _client_with_transport(handler)

    with pytest.raises(OpenCodeClientError) as exc:
        client.list_agents()

    assert exc.value.status == 500


def test_default_constructor_uses_static_defaults() -> None:
    client = OpenCodeClient()

    assert client.server_url == "http://127.0.0.1:4096"


def test_constructor_args_override_defaults() -> None:
    client = OpenCodeClient(serve_port=1234, username="u", password="p")

    assert client.server_url == "http://127.0.0.1:1234"


# ---------------------------------------------------------------------------
# R3A：长持有 httpx.Client 复用 / update_config 重建 / close
# ---------------------------------------------------------------------------


def test_http_client_returns_singleton_across_calls() -> None:
    client = OpenCodeClient()

    c1 = client.http_client()
    c2 = client.http_client()

    assert c1 is c2
    assert not c1.is_closed
    client.close()


def test_http_client_rebuilds_after_close() -> None:
    client = OpenCodeClient()
    c1 = client.http_client()

    client.close()
    c2 = client.http_client()

    assert c1 is not c2
    assert c1.is_closed
    assert not c2.is_closed
    client.close()


def test_update_config_with_real_change_detaches_old_client() -> None:
    """update_config 真实变更后 detach 旧 client（置 None，不主动 close），
    下一次 http_client() 用新参数重建。详见 opencode_client.py docstring。
    """
    client = OpenCodeClient(serve_port=4096)
    c1 = client.http_client()

    client.update_config(serve_port=5050)

    # detach 语义：旧 client 不被强行关闭（在飞请求仍可用）；新 client 用新 url
    assert not c1.is_closed
    assert client.server_url == "http://127.0.0.1:5050"
    c2 = client.http_client()
    assert c2 is not c1
    client.close()


def test_update_config_with_no_change_keeps_client() -> None:
    client = OpenCodeClient(serve_port=4096)
    c1 = client.http_client()

    client.update_config(serve_port=4096)

    assert not c1.is_closed
    c2 = client.http_client()
    assert c2 is c1
    client.close()


def test_update_config_username_password_timeout_rebuild() -> None:
    client = OpenCodeClient(username="u", password="p", timeout=5.0)
    c1 = client.http_client()

    client.update_config(username="u2", password="p2", timeout=10.0)

    # detach 语义：旧 client 未被关闭，但下次 http_client() 会新建
    assert not c1.is_closed
    assert client.username == "u2"
    assert client.password == "p2"
    c2 = client.http_client()
    assert c2 is not c1
    client.close()


def test_close_is_idempotent() -> None:
    client = OpenCodeClient()
    client.http_client()

    client.close()
    client.close()

    assert client._http_client is None



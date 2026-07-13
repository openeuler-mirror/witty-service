from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx

from witty_agent_server.infra.clients.base import ClientBase


logger = logging.getLogger(__name__)


_DEFAULT_SERVE_PORT = 4096
_DEFAULT_USERNAME = "opencode"
_DEFAULT_TIMEOUT = 30.0


class OpenCodeClientError(RuntimeError):
    """OpenCode HTTP 客户端错误。"""

    def __init__(self, *, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class OpenCodeClient(ClientBase):
    """OpenCode ``serve`` HTTP REST + SSE 客户端。

    用 ``httpx`` + HTTP Basic Auth 实现 ``ClientBase``。
    OpenCode 无 gateway-agent / agent_id 概念：``list_sessions`` 忽略 agent_id。

    ``http_client()`` 返回**长持有**的 ``httpx.Client`` 实例，跨方法复用以
    利用连接池。流式场景使用 ``stream_client()``。
    """

    def __init__(
        self,
        *,
        serve_port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._serve_port = serve_port or _DEFAULT_SERVE_PORT
        self._server_url = f"http://127.0.0.1:{self._serve_port}"
        self._username = username if username is not None else _DEFAULT_USERNAME
        self._password = password if password is not None else ""
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT
        # session_key -> opencode session.id
        self._session_map: dict[str, str] = {}
        self._http_client: httpx.Client | None = None

    @property
    def server_url(self) -> str:
        return self._server_url

    @property
    def serve_port(self) -> int:
        return self._serve_port

    @property
    def username(self) -> str:
        return self._username

    @property
    def password(self) -> str:
        return self._password

    def update_config(
        self,
        *,
        serve_port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
    ) -> None:
        """运行时更新连接参数。"""
        changed = False
        if serve_port is not None and serve_port != self._serve_port:
            self._serve_port = serve_port
            self._server_url = f"http://127.0.0.1:{serve_port}"
            changed = True
        if username is not None and username != self._username:
            self._username = username
            changed = True
        if password is not None and password != self._password:
            self._password = password
            changed = True
        if timeout is not None and timeout != self._timeout:
            self._timeout = timeout
            changed = True
        if changed:
            # detach 不 close：在飞的旧引用继续走旧 client（旧 URL/auth），
            # 不被强行关闭；后续 http_client() 用新参数重建。
            self._http_client = None

    def _build_client(self) -> httpx.Client:
        """按当前连接参数构造一个新的 ``httpx.Client``。"""
        return httpx.Client(
            base_url=self._server_url,
            auth=(self._username, self._password),
            timeout=self._timeout,
        )

    def http_client(self) -> httpx.Client:
        """返回长持有的 ``httpx.Client``；惰性构造，已关闭则重建。

        调用方不要对返回值使用 ``with``：会关闭共享实例并破坏连接复用、
        破坏后续调用。流式场景请用 ``stream_client()`` 取独立 client。
        析构时由 ``close()`` 统一回收。
        """
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = self._build_client()
        return self._http_client

    def stream_client(self) -> httpx.Client:
        """返回**每次新建**的 ``httpx.Client``，专供流式请求使用。

        与 ``http_client()`` 不同：本方法不复用、不缓存，调用方**应当用
        ``with``** 在结束时关闭实例，避免泄漏 socket。
        """
        return self._build_client()

    def close(self) -> None:
        """关闭底层 ``httpx.Client`` 并置空，下次 ``http_client()`` 重建。"""
        client = self._http_client
        if client is None:
            return
        self._http_client = None
        if not client.is_closed:
            try:
                client.close()
            except Exception:
                logger.debug("ignored error while closing httpx.Client", exc_info=True)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def list_agents(self) -> dict[str, Any]:
        response = self.http_client().get("/agent")
        self._raise_for_status(response, action="list_agents")
        try:
            agents = response.json()
        except (ValueError, httpx.HTTPError):
            raise OpenCodeClientError(
                status=response.status_code,
                message="opencode list_agents failed: invalid JSON response",
            )
        if not isinstance(agents, list):
            agents = []
        default_id = agents[0].get("id", "main") if agents and isinstance(agents[0], dict) else "main"
        return {
            "defaultId": default_id,
            "agents": agents,
        }

    def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
        del agent_id
        response = self.http_client().get("/session")
        self._raise_for_status(response, action="list_sessions")
        try:
            body = response.json()
        except (ValueError, httpx.HTTPError):
            raise OpenCodeClientError(
                status=response.status_code,
                message="opencode list_sessions failed: invalid JSON response",
            )
        sessions = body if isinstance(body, list) else []
        return {"sessions": sessions}

    def get_agent(self, *, agent_id: str) -> dict[str, Any] | None:
        raise NotImplementedError(
            "use lifecycle.probe_running() for readiness"
        )

    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """opencode skill 系统 endpoint 待后续实现"""
        raise NotImplementedError(
            "OpenCode skills status endpoint not yet wired; "
        )

    def create_session(self, *, session_key: str) -> None:
        response = self.http_client().post("/session", json={"title": session_key})
        self._raise_for_status(response, action="create_session")
        try:
            body = response.json()
        except (ValueError, httpx.HTTPError):
            raise OpenCodeClientError(
                status=response.status_code,
                message="opencode create_session failed: invalid JSON response",
            )
        session_id = body.get("id") if isinstance(body, dict) else None
        if isinstance(session_id, str) and session_id:
            self._session_map[session_key] = session_id

    def delete_session(self, *, session_key: str) -> None:
        session_id = self._resolve_session_id_or_lookup(session_key)
        response = self.http_client().delete(f"/session/{session_id}")
        self._raise_for_status(response, action="delete_session")
        # 删除成功后清理 mapping
        self._session_map.pop(session_key, None)

    def abort_session(self, *, session_key: str) -> None:
        session_id = self._resolve_session_id_or_lookup(session_key)
        response = self.http_client().post(f"/session/{session_id}/abort")
        self._raise_for_status(response, action="abort_session")

    def stream_turn(
        self, *, session_key: str, message: str
    ) -> Iterator[dict[str, Any]]:
        """流式执行单轮。

        SSE 事件映射待实现。实现时请使用 ``stream_client()`` 
        """
        raise NotImplementedError(
            "OpenCodeClient.stream_turn is implemented in M4 (SSE event mapping)"
        )

    def _resolve_session_id_or_lookup(self, session_key: str) -> str:
        """解析 session_key 对应的 opencode session.id。

        优先走内存 mapping(create_session 时写入),miss 时通过
        ``GET /session`` 列表按 ``title == session_key`` 回查
        """
        session_id = self._session_map.get(session_key)
        if isinstance(session_id, str) and session_id:
            return session_id

        session_id = self._lookup_session_id_by_title(session_key)
        if isinstance(session_id, str) and session_id:
            self._session_map[session_key] = session_id
            return session_id

        raise OpenCodeClientError(
            status=409,
            message=(
                f"cannot resolve opencode session id for session_key={session_key!r}; "
                "not in memory mapping and not found in opencode /session list "
            ),
        )

    def _lookup_session_id_by_title(self, session_key: str) -> str | None:
        try:
            payload = self.list_sessions(agent_id="")
        except (OpenCodeClientError, ValueError) as exc:
            # ValueError: list_sessions response 非 JSON / JSON 非 list
            logger.debug(
                "session id lookup via list_sessions failed: session_key=%s err=%s",
                session_key,
                exc,
            )
            return None
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, list):
            return None
        for item in sessions:
            if not isinstance(item, dict):
                continue
            if item.get("title") == session_key:
                raw_id = item.get("id")
                if isinstance(raw_id, str) and raw_id:
                    return raw_id
        return None

    def resolve_session_id(self, session_key: str) -> str | None:
        """返回 session_key 对应的 opencode session.id（若有映射）。"""
        return self._session_map.get(session_key)

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, action: str) -> None:
        if response.status_code >= 400:
            raise OpenCodeClientError(
                status=response.status_code,
                message=f"opencode {action} failed: HTTP {response.status_code} {response.text}",
            )


__all__ = ["OpenCodeClient", "OpenCodeClientError"]

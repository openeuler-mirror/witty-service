from __future__ import annotations

import httpx
from typing import Any


class AdaptorHttpClient:
    """HTTP 客户端，用于调用 witty-agent-server API"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送 POST 请求"""
        client = await self._get_client()
        response = await client.post(path, json=json)
        response.raise_for_status()
        return response.json()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送 GET 请求"""
        client = await self._get_client()
        response = await client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def delete(self, path: str) -> None:
        """发送 DELETE 请求"""
        client = await self._get_client()
        response = await client.delete(path)
        response.raise_for_status()

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            client = await self._get_client()
            response = await client.get("/v1/ping")
            return response.status_code == 200
        except Exception:
            return False
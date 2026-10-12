from __future__ import annotations

from typing import Any

import httpx

QueryParams = dict[str, Any] | list[tuple[str, Any]]


class AdaptorHttpClient:
    """Async HTTP client shared by runtime adaptor and insight upstream calls."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._default_headers = dict(default_headers or {})
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                headers=self._default_headers,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        client = await self._get_client()
        response = await client.request(
            method,
            path,
            params=params,
            json=json,
            timeout=timeout,
        )
        response.raise_for_status()
        return response

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> Any:
        response = await self._request(
            method,
            path,
            params=params,
            json=json,
            timeout=timeout,
        )
        if getattr(response, "content", None) == b"":
            return None
        return response.json()

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
        params: QueryParams | None = None,
    ) -> Any:
        return await self._request_json(
            "POST", path, params=params, json=json, timeout=timeout
        )

    async def get(
        self,
        path: str,
        params: QueryParams | None = None,
    ) -> Any:
        return await self._request_json("GET", path, params=params)

    async def delete(self, path: str) -> Any:
        return await self._request_json("DELETE", path)

    async def list_agents(self) -> dict[str, Any]:
        payload = await self.get("/agent/list")
        return payload if isinstance(payload, dict) else {}

    async def health_check(self) -> bool:
        try:
            response = await self._request("GET", "/ping")
            return response.status_code == 200
        except Exception:
            return False

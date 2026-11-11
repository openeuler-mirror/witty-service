from __future__ import annotations

from typing import Any

import httpx

_JSON_TYPES = {"application/json", "application/problem+json"}


class OpenCodeV2Error(Exception):
    def __init__(self, *, status_code: int, tag: str | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.tag = tag
        self.message = message


class OpenCodeV2Client:
    """OpenCode 1.17.13 v2 ``/api`` 面的最小同步封装。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def healthy(self) -> bool:
        try:
            body = await self._get_json("/api/health")
        except (OpenCodeV2Error, httpx.HTTPError):
            return False
        return body.get("healthy") is True

    async def create_session(
        self, *, directory: str, agent: str | None = None, model: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"location": {"directory": directory}}
        if agent is not None:
            payload["agent"] = agent
        if model is not None:
            payload["model"] = model
        body = await self._post_json("/api/session", json=payload)
        return _data_object(body)

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            body = await self._get_json(f"/api/session/{session_id}")
        except OpenCodeV2Error as exc:
            if exc.status_code == 404:
                return None
            raise
        return _data_object(body)

    async def get_location(self, *, directory: str) -> dict[str, Any]:
        return await self._get_json(
            "/api/location", params={"location[directory]": directory}
        )

    async def list_agents(self, *, directory: str) -> list[dict[str, Any]]:
        return await self._get_data_list("/api/agent", directory=directory)

    async def list_models(self, *, directory: str) -> list[dict[str, Any]]:
        return await self._get_data_list("/api/model", directory=directory)

    async def list_skills(self, *, directory: str) -> list[dict[str, Any]]:
        return await self._get_data_list("/api/skill", directory=directory)

    async def _get_data_list(
        self, path: str, *, directory: str
    ) -> list[dict[str, Any]]:
        body = await self._get_json(path, params={"location[directory]": directory})
        data = body.get("data")
        if not isinstance(data, list):
            raise OpenCodeV2Error(
                status_code=502,
                tag=None,
                message=f"{path} returned an invalid data payload",
            )
        return [item for item in data if isinstance(item, dict)]

    async def _get_json(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        response = await self._http.get(path, params=params)
        return _decode_json(response)

    async def _post_json(
        self, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._http.post(path, json=json)
        return _decode_json(response)


def _decode_json(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in _JSON_TYPES:
        raise OpenCodeV2Error(
            status_code=response.status_code,
            tag=None,
            message=f"expected JSON response, got {media_type or 'no content-type'}",
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise OpenCodeV2Error(
            status_code=response.status_code,
            tag=None,
            message="response body is not valid JSON",
        ) from exc
    if not isinstance(body, dict):
        raise OpenCodeV2Error(
            status_code=response.status_code,
            tag=None,
            message="response body must be a JSON object",
        )
    if response.status_code >= 400:
        raise OpenCodeV2Error(
            status_code=response.status_code,
            tag=_optional_text(body.get("_tag")),
            message=_optional_text(body.get("message")) or response.reason_phrase,
        )
    return body


def _data_object(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data")
    if not isinstance(data, dict):
        raise OpenCodeV2Error(
            status_code=502, tag=None, message="response did not contain a data object"
        )
    return data


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

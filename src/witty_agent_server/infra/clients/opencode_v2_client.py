from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

_JSON_TYPES = {"application/json", "application/problem+json"}
_MAX_PAGE_LIMIT = 100


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
        self,
        *,
        directory: str,
        agent: str | None = None,
        model: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"location": {"directory": directory}}
        if agent is not None:
            payload["agent"] = agent
        if model is not None:
            payload["model"] = model
        body = await self._post_json("/api/session", json=payload)
        return _data_object(body)

    async def prompt(
        self,
        session_id: str,
        *,
        message_id: str,
        text: str,
        resume: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": message_id, "prompt": {"text": text}}
        if resume is not None:
            payload["resume"] = resume
        body = await self._post_json(f"/api/session/{session_id}/prompt", json=payload)
        return _data_object(body)

    async def switch_agent(self, session_id: str, *, agent: str) -> None:
        await self._post_no_content(
            f"/api/session/{session_id}/agent", json={"agent": agent}
        )

    async def switch_model(self, session_id: str, *, model: dict[str, str]) -> None:
        await self._post_no_content(
            f"/api/session/{session_id}/model", json={"model": model}
        )

    async def history(
        self, session_id: str, *, after_seq: int, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        body = await self._get_json(
            f"/api/session/{session_id}/history",
            params={
                "after": str(after_seq),
                "limit": str(max(1, min(limit, _MAX_PAGE_LIMIT))),
            },
        )
        return _data_list(body, "/api/session/history"), body.get("hasMore") is True

    async def messages(
        self, session_id: str, *, limit: int, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        params: dict[str, str] = {"limit": str(max(1, min(limit, _MAX_PAGE_LIMIT)))}
        if cursor is not None:
            params["cursor"] = cursor
        body = await self._get_json(f"/api/session/{session_id}/message", params=params)
        cursors = body.get("cursor")
        return _data_list(body, "/api/session/message"), {
            key: value
            for key, value in (cursors.items() if isinstance(cursors, dict) else ())
            if isinstance(value, str) and value
        }

    async def stream_events(
        self, *, after_seq: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        params = {"after": str(after_seq)} if after_seq is not None else None
        async with self._http.stream(
            "GET", "/api/event", params=params, timeout=httpx.Timeout(5.0, read=None)
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise OpenCodeV2Error(
                    status_code=response.status_code,
                    tag=None,
                    message="event stream was rejected",
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if not chunk:
                    continue
                try:
                    payload = json.loads(chunk)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    yield payload

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
        return _data_list(body, path)

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

    async def _post_no_content(
        self, path: str, *, json: dict[str, Any] | None = None
    ) -> None:
        response = await self._http.post(path, json=json)
        if response.status_code >= 400:
            _decode_json(response)


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


def _data_list(body: dict[str, Any], path: str) -> list[dict[str, Any]]:
    data = body.get("data")
    if not isinstance(data, list):
        raise OpenCodeV2Error(
            status_code=502,
            tag=None,
            message=f"{path} returned an invalid data payload",
        )
    return [item for item in data if isinstance(item, dict)]


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None

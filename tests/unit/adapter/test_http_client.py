from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from witty_service.adapter.http_client import AdaptorHttpClient


@pytest.fixture()
def client() -> AdaptorHttpClient:
    return AdaptorHttpClient(
        base_url="http://localhost:8080/",
        timeout=5.0,
        default_headers={"Authorization": "Bearer secret-token"},
    )


def test_client_initialization_stores_base_configuration(
    client: AdaptorHttpClient,
) -> None:
    assert client.base_url == "http://localhost:8080"
    assert client._timeout == 5.0
    assert client._default_headers == {"Authorization": "Bearer secret-token"}
    assert client._client is None


@pytest.mark.asyncio
async def test_get_client_creates_async_client_with_default_headers(
    client: AdaptorHttpClient,
) -> None:
    with patch("witty_service.adapter.http_client.httpx.AsyncClient") as client_class:
        client_instance = AsyncMock()
        client_class.return_value = client_instance

        result = await client._get_client()

    client_class.assert_called_once_with(
        base_url="http://localhost:8080",
        timeout=5.0,
        headers={"Authorization": "Bearer secret-token"},
    )
    assert result is client_instance


@pytest.mark.asyncio
async def test_request_uses_single_request_entrypoint(
    client: AdaptorHttpClient,
) -> None:
    request_client = AsyncMock()
    response = MagicMock()
    request_client.request.return_value = response
    client._client = request_client

    result = await client._request(
        "POST",
        "/api/test",
        params={"page": 1},
        json={"message": "hello"},
        timeout=9.0,
    )

    request_client.request.assert_called_once_with(
        "POST",
        "/api/test",
        params={"page": 1},
        json={"message": "hello"},
        timeout=9.0,
    )
    response.raise_for_status.assert_called_once_with()
    assert result is response


@pytest.mark.asyncio
async def test_request_json_returns_parsed_payload(client: AdaptorHttpClient) -> None:
    request_client = AsyncMock()
    response = MagicMock()
    response.json.return_value = {"ok": True}
    request_client.request.return_value = response
    client._client = request_client

    result = await client._request_json("GET", "/api/test", params={"scope": "all"})

    assert result == {"ok": True}
    request_client.request.assert_called_once_with(
        "GET",
        "/api/test",
        params={"scope": "all"},
        json=None,
        timeout=None,
    )


@pytest.mark.asyncio
async def test_get_post_and_delete_delegate_to_request_json(
    client: AdaptorHttpClient,
) -> None:
    with patch.object(
        client,
        "_request_json",
        new=AsyncMock(side_effect=[{"a": 1}, {"b": 2}, {"c": 3}]),
    ) as request_json:
        get_result = await client.get("/api/items", params={"page": 1})
        post_result = await client.post(
            "/api/items", json={"name": "test"}, timeout=10.0
        )
        delete_result = await client.delete("/api/items/1")

    assert get_result == {"a": 1}
    assert post_result == {"b": 2}
    assert delete_result == {"c": 3}
    assert request_json.await_args_list == [
        (("GET", "/api/items"), {"params": {"page": 1}}),
        (
            ("POST", "/api/items"),
            {"json": {"name": "test"}, "timeout": 10.0, "params": None},
        ),
        (("DELETE", "/api/items/1"), {}),
    ]


@pytest.mark.asyncio
async def test_list_agents_uses_existing_runtime_endpoint(
    client: AdaptorHttpClient,
) -> None:
    with patch.object(client, "get", new=AsyncMock(return_value={"agents": []})) as get:
        result = await client.list_agents()

    assert result == {"agents": []}
    get.assert_awaited_once_with("/agent/list")


@pytest.mark.asyncio
async def test_health_check_returns_true_for_200(client: AdaptorHttpClient) -> None:
    response = MagicMock(status_code=200)
    with patch.object(
        client, "_request", new=AsyncMock(return_value=response)
    ) as request:
        result = await client.health_check()

    assert result is True
    request.assert_awaited_once_with("GET", "/ping")


@pytest.mark.asyncio
async def test_health_check_returns_false_for_non_200(
    client: AdaptorHttpClient,
) -> None:
    response = MagicMock(status_code=503)
    with patch.object(client, "_request", new=AsyncMock(return_value=response)):
        result = await client.health_check()

    assert result is False


@pytest.mark.asyncio
async def test_health_check_returns_false_for_transport_error(
    client: AdaptorHttpClient,
) -> None:
    with patch.object(
        client, "_request", new=AsyncMock(side_effect=httpx.ConnectError("boom"))
    ):
        result = await client.health_check()

    assert result is False


@pytest.mark.asyncio
async def test_close_closes_client_and_resets_instance(
    client: AdaptorHttpClient,
) -> None:
    request_client = AsyncMock()
    client._client = request_client

    await client.close()

    request_client.aclose.assert_awaited_once_with()
    assert client._client is None

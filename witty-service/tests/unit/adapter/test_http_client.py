import asyncio
import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.adapter.http_client import AdaptorHttpClient


def test_client_initialization():
    """测试客户端初始化"""
    client = AdaptorHttpClient(base_url="http://localhost:8080")
    assert client.base_url == "http://localhost:8080"
    assert client._client is None


def test_client_strips_trailing_slash():
    """测试 base_url 末尾斜杠被移除"""
    client = AdaptorHttpClient(base_url="http://localhost:8080/")
    assert client.base_url == "http://localhost:8080"


def test_get_client_creates_client_on_first_call():
    """测试 _get_client 在首次调用时创建客户端"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_class.return_value = mock_client_instance

            result = await client._get_client()

            mock_client_class.assert_called_once_with(
                base_url="http://localhost:8080", timeout=30.0
            )
            assert result == mock_client_instance
            assert client._client is mock_client_instance

    asyncio.run(run())


def test_get_client_returns_same_client_on_subsequent_calls():
    """测试 _get_client 在后续调用时返回相同客户端"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_class.return_value = mock_client_instance

            result1 = await client._get_client()
            result2 = await client._get_client()

            assert result1 == result2
            mock_client_class.assert_called_once()

    asyncio.run(run())


def test_close_closes_client():
    """测试 close 方法关闭客户端"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        client._client = mock_client

        await client.close()

        mock_client.aclose.assert_called_once()
        assert client._client is None

    asyncio.run(run())


def test_close_does_nothing_when_client_is_none():
    """测试 close 在客户端为 None 时不执行操作"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        assert client._client is None

        await client.close()

        assert client._client is None

    asyncio.run(run())


def test_post_success():
    """测试 POST 请求成功"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "ok"}
        mock_client.post.return_value = mock_response
        client._client = mock_client

        result = await client.post("/api/test", json={"key": "value"})

        mock_client.post.assert_called_once_with("/api/test", json={"key": "value"})
        mock_response.raise_for_status.assert_called_once()
        assert result == {"status": "ok"}

    asyncio.run(run())


def test_post_raises_on_error():
    """测试 POST 请求失败时抛出异常"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock()
        )
        mock_client.post.return_value = mock_response
        client._client = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await client.post("/api/test")

    asyncio.run(run())


def test_get_success():
    """测试 GET 请求成功"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": "test"}
        mock_client.get.return_value = mock_response
        client._client = mock_client

        result = await client.get("/api/test", params={"key": "value"})

        mock_client.get.assert_called_once_with("/api/test", params={"key": "value"})
        mock_response.raise_for_status.assert_called_once()
        assert result == {"data": "test"}

    asyncio.run(run())


def test_get_raises_on_error():
    """测试 GET 请求失败时抛出异常"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock()
        )
        mock_client.get.return_value = mock_response
        client._client = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/api/test")

    asyncio.run(run())


def test_delete_success():
    """测试 DELETE 请求成功"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_client.delete.return_value = mock_response
        client._client = mock_client

        await client.delete("/api/test/1")

        mock_client.delete.assert_called_once_with("/api/test/1")
        mock_response.raise_for_status.assert_called_once()

    asyncio.run(run())


def test_delete_raises_on_error():
    """测试 DELETE 请求失败时抛出异常"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock()
        )
        mock_client.delete.return_value = mock_response
        client._client = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await client.delete("/api/test/1")

    asyncio.run(run())


def test_health_check_success():
    """测试健康检查成功"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.get.return_value = mock_response
        client._client = mock_client

        result = await client.health_check()

        mock_client.get.assert_called_once_with("/v1/ping")
        assert result is True

    asyncio.run(run())


def test_health_check_failure():
    """测试健康检查失败（状态码非200）"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client.get.return_value = mock_response
        client._client = mock_client

        result = await client.health_check()

        assert result is False

    asyncio.run(run())


def test_health_check_exception():
    """测试健康检查异常时返回 False"""
    async def run() -> None:
        client = AdaptorHttpClient(base_url="http://localhost:8080")
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection error")
        client._client = mock_client

        result = await client.health_check()

        assert result is False

    asyncio.run(run())
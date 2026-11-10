"""探针共享 fixture。

普通用例共享一个 module 级 server，但每个用例使用独立 session 与唯一 marker；
需要重启 server 的用例通过 ``standalone_env`` 使用独立实例，避免中途重启共享
环境导致其他用例不可复现。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tests.probes.mock_provider import MockProvider
from tests.probes.probe_env import (
    ProbeEnv,
    probe_tmp_root,
    resolve_opencode_bin,
    verify_opencode_binary,
)

MCP_SERVER = Path(__file__).with_name("mcp_echo_server.py")


def new_marker(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="module")
def mock() -> Iterator[MockProvider]:
    provider = MockProvider()
    provider.start()
    yield provider
    provider.stop()


def _build_env(mock_provider: MockProvider) -> ProbeEnv:
    bin_path = resolve_opencode_bin()
    if bin_path is None:
        pytest.fail(
            "未找到 OpenCode 1.17.13 可执行文件；请设置 "
            "WITTY_PROBE_OPENCODE_BIN 或 WITTY_PROBE_OPENCODE_DIR",
            pytrace=False,
        )
    verify_opencode_binary(bin_path)
    root = probe_tmp_root() / f"pytest-{uuid.uuid4().hex[:12]}"
    return ProbeEnv(
        root=root,
        mock_provider_base_url=mock_provider.base_url,
        mcp_server_path=MCP_SERVER,
        bin_path=bin_path,
    )


@pytest.fixture(scope="module")
def env(mock: MockProvider) -> Iterator[ProbeEnv]:
    probe_env = _build_env(mock)
    probe_env.start()
    yield probe_env
    probe_env.stop()


@pytest.fixture(scope="module")
def client(env: ProbeEnv) -> Iterator[httpx.Client]:
    probe_client = env.client()
    yield probe_client
    probe_client.close()


@pytest.fixture()
def standalone_env(
    mock: MockProvider,
) -> Iterator[Callable[[], ProbeEnv]]:
    """工厂 fixture：每个需要独立 server 的用例显式创建、由 fixture 统一回收。"""
    created: list[ProbeEnv] = []

    def make() -> ProbeEnv:
        probe_env = _build_env(mock)
        probe_env.start()
        created.append(probe_env)
        return probe_env

    yield make
    for probe_env in reversed(created):
        probe_env.stop()


@pytest.fixture(scope="module")
def baseline_tools(env: ProbeEnv, mock: MockProvider) -> set[str]:
    """未指定 ``tools`` 时 provider 实际收到的工具集合（规范化 baseline）。

    不硬编码 11 或 14 个名字：baseline 由 pinned 版本 + 当前配置实测得到，
    后续用例只断言相对 baseline 的增删。
    """
    marker = new_marker("baseline")
    with env.client() as probe_client:
        session_id = env.create_session(probe_client, title=marker)
        env.prompt_and_wait(probe_client, session_id, text=marker)
    return set(mock.tool_names_for(marker))

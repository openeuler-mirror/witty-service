"""真实 opencode 二进制的"模型目录翻转"回归测试。

2026-09-28 事故:上游 models.dev 把 deepseek-v4-flash 标为 deprecated,
opencode 启动时把 config.provider.<id>.models 深合并进目录后 delete 该条目,
使 "deepseek/deepseek-v4-flash" 无法解析(ProviderModelNotFoundError,对外
表现为 HTTP 500 UnknownError),而 /global/health 依然 healthy、agent 状态
一直是 RUNNING -- 即"进程健康但每次调用都失败"。

本测试用真实二进制 + 事故现场的目录快照复现该场景,断言:

1. 生成的配置自带 status="active",模型在 GET /provider 里存活(对应 P0);
2. 一旦这条保护失效,start_server() 直接以 OpenCodeServeStartError 失败,
   而不是启动一个不可用的 serve(对应 P1 preflight)。

目录注入方式:OPENCODE_MODELS_PATH 指向 fixture 文件(`deepseek-v4-flash`
在快照里已是 `deprecated`,即事故现场本身)。它与
OPENCODE_DISABLE_MODELS_FETCH=1 配合可以完全离线、确定性地替换整个模型目录
(实测 /provider 只返回 fixture 里的 provider),不写缓存、不联网。

未安装 opencode 时整模块跳过。
"""

from __future__ import annotations

import shutil
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from witty_agent_server.application.services.agent.opencode_lifecycle_service import (
    OpenCodeLifecycleService,
    OpenCodeServeStartError,
)
from witty_agent_server.infra.clients.opencode_client import OpenCodeClient

pytestmark = pytest.mark.skipif(
    shutil.which("opencode") is None,
    reason="opencode binary not installed; skipping real-binary catalog-flip test",
)

# 取自线上 models.dev 目录的 deepseek provider 条目(含全部模型字段),
# 其中 deepseek-v4-flash 已被上游标为 deprecated(即事故现场)。
# opencode 对模型条目有必填字段,残缺 fixture 会让 GET /provider 直接 500。
_CATALOG_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "opencode_deepseek_catalog.json"
)

_MODEL = "deepseek-v4-flash"
_PROVIDER = "deepseek"
_PROFILE = "catalog-flip"
# 同一 provider 下的兄弟模型,用来验证 opencode 是"深合并"而非"整体替换"目录。
_SIBLING_MODELS = ("deepseek-flash", "deepseek-v4-pro")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stage_catalog(tmp_path: Path) -> Path:
    """把事故现场的目录快照复制到临时目录,避免 opencode 写回仓库 fixture。"""
    target = tmp_path / "models.json"
    shutil.copyfile(_CATALOG_FIXTURE, target)
    return target


def _provider_models(service: OpenCodeLifecycleService) -> dict[str, Any]:
    response = service.client.http_client().get("/provider", timeout=10.0)
    assert response.status_code == 200, response.text
    providers = {p["id"]: p for p in response.json()["all"]}
    assert _PROVIDER in providers, sorted(providers)
    return dict(providers[_PROVIDER]["models"])


@pytest.fixture
def lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[OpenCodeLifecycleService]:
    """真实 opencode serve,但 XDG 与模型目录全部隔离到 tmp_path。"""
    settings = MagicMock()
    settings.workspace.root_path.return_value = tmp_path
    monkeypatch.setattr(
        "witty_agent_server.application.services.agent."
        "opencode_lifecycle_service.get_settings",
        lambda: settings,
    )
    # instance_config_home 走 workspace_paths.agent_workspace_path,同样要隔离
    monkeypatch.setattr("witty_service.workspace_paths.get_settings", lambda: settings)
    monkeypatch.setenv("OPENCODE_DISABLE_MODELS_FETCH", "1")
    monkeypatch.setenv("OPENCODE_MODELS_PATH", str(_stage_catalog(tmp_path)))

    client = OpenCodeClient(serve_port=_free_port(), password="")
    service = OpenCodeLifecycleService(client=client, profile=_PROFILE)
    service.configure_model(
        model_provider=_PROVIDER,
        model_name=_MODEL,
        api_key="sk-not-a-real-key",
        api_base_url="https://api.deepseek.com",
    )
    try:
        yield service
    finally:
        # 断言失败时也必须收掉子进程,避免污染后续测试的端口/进程
        service._stop_serve_process()
        client.close()


def test_declared_model_survives_deprecated_upstream_catalog(
    lifecycle: OpenCodeLifecycleService,
) -> None:
    """核心断言:上游目录把模型标 deprecated,start_server 仍必须成功。"""
    lifecycle.start_server()

    models = _provider_models(lifecycle)
    assert _MODEL in models, f"declared model dropped from catalog: {sorted(models)}"
    assert models[_MODEL].get("status") == "active"
    # 控制面声明的是"深合并进目录",不是"整体替换目录":
    # 同一 provider 下的兄弟模型必须原样保留,否则会把用户其它可用模型删掉。
    for sibling in _SIBLING_MODELS:
        assert sibling in models, (
            f"sibling model {sibling} was dropped: {sorted(models)}"
        )


def test_pre_fix_builder_shape_fails_fast(
    lifecycle: OpenCodeLifecycleService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把 builder 换回修复前的实现,start_server 必须立刻变红。

    这是"红过的"证明:一旦有人把 models 条目里的 status 去掉(或重新挪回
    api_base_url 分支),preflight 会立刻拦住,而不是静默放行一个每次调用都
    500 的 serve。
    """
    import witty_agent_server.application.services.agent.opencode_lifecycle_service as ocls

    def pre_fix_builder(
        *,
        model_provider: str,
        model_name: str | None,
        api_key: str,
        api_base_url: str | None,
        compatibility: str | None = None,
    ) -> dict[str, Any] | None:
        if not model_provider or not model_name:
            return None
        provider: dict[str, Any] = {}
        if api_base_url:
            provider["npm"] = "@ai-sdk/openai-compatible"
            provider["options"] = {"baseURL": api_base_url, "apiKey": api_key}
            provider["models"] = {model_name: {"name": model_name}}
        return {
            "model": f"{model_provider}/{model_name}",
            "provider": {model_provider: provider},
        }

    monkeypatch.setattr(ocls, "_build_opencode_model_config", pre_fix_builder)
    lifecycle.configure_model(
        model_provider=_PROVIDER,
        model_name=_MODEL,
        api_key="sk-not-a-real-key",
        api_base_url="https://api.deepseek.com",
    )

    with pytest.raises(OpenCodeServeStartError) as exc:
        lifecycle.start_server()

    assert f"{_PROVIDER}/{_MODEL}" in exc.value.message
    assert "missing from opencode" in exc.value.message

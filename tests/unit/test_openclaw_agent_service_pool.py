"""openclaw agent service 的 provider 模板目录池（Phase 2）单元测试。

覆盖：模板命名/分配/快照/丢弃，以及 start() 中「未 provisioned 跳过
probe + 模板分配 + onboard 失败自愈」的集成路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import witty_agent_server.application.services.agent.openclaw_agent_service as svc_mod
from witty_agent_server.application.services.agent.errors import AgentServiceError
from witty_agent_server.application.services.agent.openclaw_agent_service import (
    OpenClawAgentService,
)
from witty_agent_server.application.services.agent.openclaw_lifecycle_service import (
    OpenClawLifecycleError,
)


def _patch_profile_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(
        svc_mod, "openclaw_profile_home", lambda profile: home / f".openclaw-{profile}"
    )


def _make_template(home: Path, name: str) -> Path:
    template = home / f".openclaw-{name}"
    template.mkdir(parents=True)
    (template / "openclaw.json").write_text("{}", encoding="utf-8")
    (template / "npm").mkdir()
    (template / "npm" / "node_modules").mkdir()
    return template


class _StubLifecycle:
    def __init__(
        self, *, provisioned: bool = False, onboard_error: Exception | None = None
    ) -> None:
        self.provisioned = provisioned
        self.onboard_error = onboard_error
        self.calls: list[str] = []

    def profile_is_provisioned(self) -> bool:
        return self.provisioned

    def probe_running(self) -> bool:
        self.calls.append("probe_running")
        return False

    def update_config(self, *, profile: str | None, gateway_port: int | None) -> None:
        self.calls.append("update_config")

    def onboard(self, **kwargs) -> None:
        self.calls.append("onboard")
        if self.onboard_error is not None:
            raise self.onboard_error

    def start_server(self) -> None:
        self.calls.append("start_server")


class _StubGatewayClient:
    def update_config(self, *, profile: str | None, gateway_port: int | None) -> None:
        pass

    def list_agents(self) -> dict:
        return {
            "agents": [{"id": "agent-1", "default": True}],
            "defaultId": "agent-1",
        }

    def get_agent(self, *, agent_id: str) -> dict:
        return {"id": agent_id, "loaded": True}


def _make_service(
    lifecycle: _StubLifecycle,
) -> OpenClawAgentService:
    return OpenClawAgentService(
        agent=None,
        lifecycle_service=lifecycle,
        gateway_agent_client=_StubGatewayClient(),
        profile="agent-1",
        gateway_port=39487,
    )


def _start_config() -> dict:
    return {
        "model": {"provider": "deepseek", "api_key": "k"},
        "openclaw": {"profile": "agent-1", "gateway_port": 39487},
    }


# ---------------------------------------------------------------------------
# 模板池基础操作
# ---------------------------------------------------------------------------


def test_template_profile_name_sanitizes_auth_choice() -> None:
    assert (
        OpenClawAgentService._template_profile_name("deepseek-api-key")
        == "tpl-deepseek-api-key"
    )
    assert (
        OpenClawAgentService._template_profile_name("Kimi Code+API")
        == "tpl-kimi-code-api"
    )
    assert OpenClawAgentService._template_profile_name("---") == "tpl-default"


def test_allocate_profile_from_template_copies(tmp_path: Path, monkeypatch) -> None:
    _patch_profile_home(monkeypatch, tmp_path)
    template = _make_template(tmp_path, "tpl-deepseek-api-key")

    svc = _make_service(_StubLifecycle())
    assert svc._allocate_profile_from_template(auth_choice="deepseek-api-key") is True

    profile_home = tmp_path / ".openclaw-agent-1"
    assert (profile_home / "openclaw.json").exists()
    assert (profile_home / "npm" / "node_modules").exists()
    assert template.exists()  # 模板本身保留（可复用）


def test_allocate_skips_incomplete_template(tmp_path: Path, monkeypatch) -> None:
    """缺 npm（插件未装）的模板没有加速价值，回退慢路径。"""
    _patch_profile_home(monkeypatch, tmp_path)
    template = tmp_path / ".openclaw-tpl-deepseek-api-key"
    template.mkdir()
    (template / "openclaw.json").write_text("{}", encoding="utf-8")

    svc = _make_service(_StubLifecycle())
    assert svc._allocate_profile_from_template(auth_choice="deepseek-api-key") is False
    assert not (tmp_path / ".openclaw-agent-1").exists()


def test_snapshot_profile_template_excludes_runtime_state(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_profile_home(monkeypatch, tmp_path)
    profile_home = tmp_path / ".openclaw-agent-1"
    profile_home.mkdir()
    (profile_home / "openclaw.json").write_text("{}", encoding="utf-8")
    (profile_home / "state").mkdir()
    (profile_home / "logs").mkdir()
    (profile_home / "workspace-attestations").mkdir()
    (profile_home / "npm").mkdir()

    svc = _make_service(_StubLifecycle())
    svc._snapshot_profile_template(auth_choice="deepseek-api-key")

    template = tmp_path / ".openclaw-tpl-deepseek-api-key"
    assert (template / "openclaw.json").exists()
    assert (template / "npm").exists()
    assert not (template / "state").exists()
    assert not (template / "logs").exists()
    assert not (template / "workspace-attestations").exists()


def test_snapshot_profile_template_first_wins(tmp_path: Path, monkeypatch) -> None:
    _patch_profile_home(monkeypatch, tmp_path)
    template = _make_template(tmp_path, "tpl-deepseek-api-key")
    (template / "first.txt").write_text("first", encoding="utf-8")

    profile_home = tmp_path / ".openclaw-agent-1"
    profile_home.mkdir()
    (profile_home / "openclaw.json").write_text("{}", encoding="utf-8")

    svc = _make_service(_StubLifecycle())
    svc._snapshot_profile_template(auth_choice="deepseek-api-key")

    assert (template / "first.txt").exists()  # 已有模板不被覆盖


def test_discard_profile_template(tmp_path: Path, monkeypatch) -> None:
    _patch_profile_home(monkeypatch, tmp_path)
    template = _make_template(tmp_path, "tpl-deepseek-api-key")

    svc = _make_service(_StubLifecycle())
    svc._discard_profile_template(auth_choice="deepseek-api-key")

    assert not template.exists()


# ---------------------------------------------------------------------------
# start() 集成：probe 跳过 + 模板分配 + 失败自愈
# ---------------------------------------------------------------------------


def test_start_allocates_template_and_skips_probe(tmp_path: Path, monkeypatch) -> None:
    """未 provisioned：跳过 ~1.2s probe，用模板免 17s npm 安装。"""
    _patch_profile_home(monkeypatch, tmp_path)
    _make_template(tmp_path, "tpl-deepseek-api-key")

    lifecycle = _StubLifecycle(provisioned=False)
    svc = _make_service(lifecycle)

    agent = svc.start(agent_id="agent-1", config=_start_config())

    assert agent.id == "agent-1"
    assert "probe_running" not in lifecycle.calls
    assert "onboard" in lifecycle.calls
    assert "start_server" in lifecycle.calls
    assert (tmp_path / ".openclaw-agent-1" / "npm" / "node_modules").exists()


def test_start_syncs_profile_from_payload(tmp_path: Path, monkeypatch) -> None:
    """回归：runtime_factory 构造 service 时不传 profile（self._profile=None），
    start() 必须先从 payload 同步 profile，模板池快照/分配才能生效。"""
    _patch_profile_home(monkeypatch, tmp_path)
    _make_template(tmp_path, "tpl-deepseek-api-key")

    lifecycle = _StubLifecycle(provisioned=False)
    # 与 runtime_factory.create_openclaw_bundle 一致：不带 profile/gateway_port
    svc = OpenClawAgentService(
        agent=None,
        lifecycle_service=lifecycle,
        gateway_agent_client=_StubGatewayClient(),
    )

    agent = svc.start(agent_id="agent-1", config=_start_config())

    assert agent.id == "agent-1"
    # 模板被成功分配到 agent profile（self._profile 已同步）
    assert (tmp_path / ".openclaw-agent-1" / "npm" / "node_modules").exists()
    assert "onboard" in lifecycle.calls


def test_start_onboard_failure_discards_template(tmp_path: Path, monkeypatch) -> None:
    """模板分配后 onboard 仍失败 → 丢弃模板（防陈旧模板持续卡死 create）。"""
    _patch_profile_home(monkeypatch, tmp_path)
    _make_template(tmp_path, "tpl-deepseek-api-key")

    error = OpenClawLifecycleError(
        action="onboard",
        command=["openclaw"],
        returncode=1,
        stdout="",
        stderr="boom",
        message="onboard failed",
    )
    lifecycle = _StubLifecycle(provisioned=False, onboard_error=error)
    svc = _make_service(lifecycle)

    with pytest.raises(AgentServiceError):
        svc.start(agent_id="agent-1", config=_start_config())

    assert not (tmp_path / ".openclaw-tpl-deepseek-api-key").exists()


def test_start_provisioned_profile_still_probes(tmp_path: Path, monkeypatch) -> None:
    """已 provisioned（resume 场景）仍需 probe 判断 gateway 是否在运行。"""
    _patch_profile_home(monkeypatch, tmp_path)

    lifecycle = _StubLifecycle(provisioned=True)
    svc = _make_service(lifecycle)

    profile_home = tmp_path / ".openclaw-agent-1"
    profile_home.mkdir()
    (profile_home / "openclaw.json").write_text("{}", encoding="utf-8")
    (profile_home / "npm").mkdir()

    svc.start(agent_id="agent-1", config=_start_config())

    assert "probe_running" in lifecycle.calls
    # 快照跳过：模板不存在 → 顺便建一份
    assert (tmp_path / ".openclaw-tpl-deepseek-api-key").exists()

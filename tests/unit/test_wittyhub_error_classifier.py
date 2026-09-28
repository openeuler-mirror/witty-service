"""wittyhub 失败分类器与 _run_wittyhub_command 错误转换的单元测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from witty_agent_server.application.services.skill.base import (
    WITTYHUB_COMMAND_TIMEOUT_SECONDS,
)
from witty_agent_server.application.services.skill.errors import (
    OpenCodeSkillsInstallError,
    WittyHubHubHttpError,
    WittyHubRepoNotIndexedError,
    WittyHubSkillNotFoundError,
)
from witty_agent_server.application.services.skill.openclaw_skill_service import (
    OpenClawSkillService,
)
from witty_agent_server.application.services.skill.opencode_skill_service import (
    OpenCodeSkillService,
)
from witty_agent_server.application.services.skill.wittyhub_errors import (
    RAW_OUTPUT_MAX_LENGTH,
    classify_wittyhub_failure,
    extract_core_message,
    sanitize_wittyhub_output,
)
from witty_service import workspace_paths as resolver_mod

# 复现自 wittyhub@0.0.5 实际失败输出（ASCII banner + 错误文本）
BANNER = "\n".join(
    [
        "\x1b[36m╔════════════════════════════╗\x1b[0m",
        "\x1b[36m║ ▄▄ ▄▄▄ ▄▄▄ ▄▄▄▄ ▄▄▄ ▄▄▄ ║\x1b[0m",
        "╚════════════════════════════╝",
    ]
)


def _classify(stdout: str = "", stderr: str = "", returncode: int = 1):
    return classify_wittyhub_failure(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        skill_name="vmcore-analysis",
        skill_source="https://gitcode.com/openeuler/IB_Robot",
    )


# ---------------------------------------------------------------- sanitizer


def test_sanitize_strips_ansi_and_banner_lines() -> None:
    output = BANNER + "\n未找到匹配 --skill 的技能: vmcore-analysis\n"
    cleaned = sanitize_wittyhub_output(output)

    assert "未找到匹配 --skill 的技能: vmcore-analysis" in cleaned
    assert "\x1b" not in cleaned
    assert "╔" not in cleaned
    assert "║" not in cleaned


def test_sanitize_truncates_long_output() -> None:
    output = "\n".join(f"line-{i} " + "x" * 100 for i in range(50))
    cleaned = sanitize_wittyhub_output(output)

    assert len(cleaned) <= RAW_OUTPUT_MAX_LENGTH + len("...(truncated)")
    assert cleaned.endswith("...(truncated)")


def test_extract_core_message_returns_last_meaningful_line() -> None:
    output = BANNER + "\n第一行提示\n核心错误: boom\n"
    assert extract_core_message(output) == "核心错误: boom"


def test_sanitize_empty_output() -> None:
    assert sanitize_wittyhub_output("") == ""
    assert extract_core_message("") == ""


# --------------------------------------------------------------- classifier


def test_classify_skill_not_found_repo_skill_mismatch() -> None:
    code, message, raw_output = _classify(
        stdout=BANNER + "\n未找到匹配 --skill 的技能: vmcore-analysis\n"
    )

    assert code == "WITTYHUB_SKILL_NOT_FOUND"
    assert 'skill "vmcore-analysis"' in message
    assert '"https://gitcode.com/openeuler/IB_Robot"' in message
    assert raw_output
    assert "╔" not in raw_output


def test_classify_skill_not_found_name_match() -> None:
    code, message, _ = _classify(
        stdout='未找到名称匹配 "artifact-engineering" 的技能。'
    )

    assert code == "WITTYHUB_SKILL_NOT_FOUND"
    assert 'skill "vmcore-analysis"' in message


def test_classify_skill_not_found_english() -> None:
    code, _, _ = _classify(stdout="No matching skills found")

    assert code == "WITTYHUB_SKILL_NOT_FOUND"


def test_classify_repo_not_indexed_takes_precedence_over_not_found() -> None:
    code, message, _ = _classify(
        stdout="仓库 https://gitcode.com/openeuler/IB_Robot 下未找到已收录的技能"
    )

    assert code == "WITTYHUB_REPO_NOT_INDEXED"
    assert "not indexed" in message


def test_classify_hub_unreachable() -> None:
    code, message, _ = _classify(stdout="无法连接搜索服务，请检查网络")

    assert code == "WITTYHUB_HUB_UNREACHABLE"
    assert "unable to reach the skill hub service" in message


def test_classify_hub_http_error_with_status() -> None:
    code, message, _ = _classify(stdout="搜索服务返回 HTTP 502")

    assert code == "WITTYHUB_HUB_HTTP_ERROR"
    assert "502" in message


def test_classify_bad_source() -> None:
    code, message, _ = _classify(stdout="Local path does not exist: /tmp/missing")

    assert code == "WITTYHUB_BAD_SOURCE"
    assert "local path does not exist" in message


def test_classify_unmatched_falls_back_to_sanitized_last_line() -> None:
    code, message, raw_output = _classify(stdout=BANNER + "\n某个未知错误\n")

    assert code is None
    assert message == "某个未知错误"
    assert raw_output


def test_classify_empty_output_falls_back_to_exit_code() -> None:
    code, message, raw_output = _classify(returncode=3)

    assert code is None
    assert message == "wittyhub exited with code 3"
    assert raw_output == ""


# ------------------------------------------------- _run_wittyhub_command


def _raise_called_process_error(stdout: str, stderr: str = "") -> None:
    raise subprocess.CalledProcessError(
        returncode=1, cmd=["npx", "wittyhub"], output=stdout, stderr=stderr
    )


def test_run_wittyhub_command_raises_classified_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args, **kwargs):
        _raise_called_process_error(
            BANNER + "\n未找到匹配 --skill 的技能: vmcore-analysis\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    service = OpenClawSkillService()

    with pytest.raises(WittyHubSkillNotFoundError) as exc_info:
        service._run_wittyhub_command(
            ["npx", "wittyhub", "add", "src", "--skill", "s"],
            cwd=tmp_path,
            skill_name="vmcore-analysis",
            skill_source="https://gitcode.com/openeuler/IB_Robot",
            error_cls=OpenCodeSkillsInstallError,
        )

    assert exc_info.value.code == "WITTYHUB_SKILL_NOT_FOUND"
    details = exc_info.value.details
    assert "was not found" in details["reason"]
    assert "╔" not in details["raw_output"]
    assert details["skill_source"] == "https://gitcode.com/openeuler/IB_Robot"


def test_run_wittyhub_command_unmatched_uses_fallback_error_cls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args, **kwargs):
        _raise_called_process_error("某个未知错误\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    service = OpenClawSkillService()

    with pytest.raises(OpenCodeSkillsInstallError) as exc_info:
        service._run_wittyhub_command(
            ["npx", "wittyhub", "add", "src"],
            cwd=tmp_path,
            skill_name="s",
            error_cls=OpenCodeSkillsInstallError,
        )

    assert exc_info.value.details["reason"] == "某个未知错误"


def test_run_wittyhub_command_timeout_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["npx", "wittyhub"], timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    service = OpenClawSkillService()

    with pytest.raises(OpenCodeSkillsInstallError) as exc_info:
        service._run_wittyhub_command(
            ["npx", "wittyhub", "add", "src"],
            cwd=tmp_path,
            skill_name="s",
            error_cls=OpenCodeSkillsInstallError,
            timeout=30,
        )

    reason = exc_info.value.details["reason"]
    assert "timed out after 30s" in reason
    assert "skill hub service is slow" in reason


def test_run_wittyhub_command_raise_on_error_false_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args, **kwargs):
        _raise_called_process_error("未找到匹配 --skill 的技能: x")

    monkeypatch.setattr(subprocess, "run", fake_run)
    service = OpenClawSkillService()

    result = service._run_wittyhub_command(
        ["npx", "wittyhub", "remove", "x"],
        cwd=tmp_path,
        skill_name="x",
        error_cls=OpenCodeSkillsInstallError,
        raise_on_error=False,
    )

    assert result is None


# ----------------------------------------- install_skill error propagation


@pytest.mark.parametrize(
    ("service_factory", "install_kwargs"),
    [
        (
            lambda: OpenClawSkillService(),
            {
                "agent_id": "agent-1",
                "skill_name": "vmcore-analysis",
                "source_type": "wittyhub",
                "skill_source": "https://gitcode.com/openeuler/IB_Robot",
            },
        ),
        (
            OpenCodeSkillService,
            {
                "agent_id": "agent-1",
                "skill_name": "vmcore-analysis",
                "source_type": "wittyhub",
                "skill_source": "https://gitcode.com/openeuler/IB_Robot",
            },
        ),
    ],
)
def test_install_wittyhub_skill_propagates_classified_error(
    service_factory,
    install_kwargs,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WittyHub*Error 不得被 install 外层 except Exception 重新包装。"""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    mock_settings = MagicMock()
    mock_settings.workspace.root_path.return_value = home
    monkeypatch.setattr(resolver_mod, "get_settings", lambda: mock_settings)

    def fake_run(*args, **kwargs):
        # install 路径必须把统一超时常量传给 subprocess.run
        assert kwargs.get("timeout") == WITTYHUB_COMMAND_TIMEOUT_SECONDS
        _raise_called_process_error("未找到匹配 --skill 的技能: vmcore-analysis\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    service = service_factory()
    with pytest.raises(WittyHubSkillNotFoundError) as exc_info:
        service.install_skill(**install_kwargs)

    assert "was not found" in exc_info.value.details["reason"]
    assert exc_info.value.details["skill_source"] == install_kwargs["skill_source"]
    assert exc_info.value.status_code == 404


def test_install_wittyhub_skill_propagates_repo_not_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    mock_settings = MagicMock()
    mock_settings.workspace.root_path.return_value = home
    monkeypatch.setattr(resolver_mod, "get_settings", lambda: mock_settings)

    def fake_run(*args, **kwargs):
        _raise_called_process_error(
            "仓库 https://gitcode.com/openeuler/IB_Robot 下未找到已收录的技能\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    service = OpenCodeSkillService()
    with pytest.raises(WittyHubRepoNotIndexedError) as exc_info:
        service.install_skill(
            agent_id="agent-1",
            skill_name="vmcore-analysis",
            source_type="wittyhub",
            skill_source="https://gitcode.com/openeuler/IB_Robot",
        )

    assert "not indexed" in exc_info.value.details["reason"]


def test_install_wittyhub_skill_propagates_hub_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    mock_settings = MagicMock()
    mock_settings.workspace.root_path.return_value = home
    monkeypatch.setattr(resolver_mod, "get_settings", lambda: mock_settings)

    def fake_run(*args, **kwargs):
        _raise_called_process_error("搜索服务返回 HTTP 502\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    service = OpenCodeSkillService()
    with pytest.raises(WittyHubHubHttpError):
        service.install_skill(
            agent_id="agent-1",
            skill_name="vmcore-analysis",
            source_type="wittyhub",
            skill_source="convert-web-app",
        )

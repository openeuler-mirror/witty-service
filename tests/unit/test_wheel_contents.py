from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "scripts"
    / "check_wheel_contents.py"
)


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_wheel_contents", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _write_wheel(
    path: Path,
    *,
    modules: list[str],
    metadata_requires: list[str] | None = None,
) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for module in modules:
            archive.writestr(module, "")
        if metadata_requires is not None:
            lines = ["Metadata-Version: 2.1", "Name: witty-service", "Version: 0.1.0"]
            lines.extend(f"Requires-Dist: {req}" for req in metadata_requires)
            archive.writestr(
                "witty_service-0.1.0.dist-info/METADATA",
                "\n".join(lines) + "\n",
            )
    return path


def _make_src(tmp_path: Path, relative_files: list[str]) -> Path:
    src = tmp_path / "src"
    for relative in relative_files:
        target = src / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return src


def test_checker_accepts_wheel_matching_source_tree(tmp_path: Path) -> None:
    src = _make_src(
        tmp_path,
        [
            "witty_service/main.py",
            "witty_service/application/awesome_openclaw_sync.py",
            "witty_agent_server/app.py",
        ],
    )
    wheel = _write_wheel(
        tmp_path / "ok.whl",
        modules=[
            "witty_service/main.py",
            "witty_service/application/awesome_openclaw_sync.py",
            "witty_agent_server/app.py",
        ],
        metadata_requires=["fastapi>=0.115", "docker>=7.0"],
    )

    assert checker.check_wheel(wheel, src) == []


def test_checker_rejects_module_missing_from_source_tree(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["witty_service/main.py"])
    wheel = _write_wheel(
        tmp_path / "stale.whl",
        modules=["witty_service/main.py", "witty_service/sandbox/e2b.py"],
        metadata_requires=[],
    )

    problems = checker.check_wheel(wheel, src)

    assert any("src/ 已不存在的模块" in problem for problem in problems)
    assert any("sandbox/e2b.py" in problem for problem in problems)


@pytest.mark.parametrize(
    "module",
    [
        "witty_agent_server/application/materialization/converter.py",
        "witty_agent_server/application/services/agent/dsh_agent_service.py",
        "witty_agent_server/application/services/session/openclaw_session_service.py",
        "witty_agent_server/infra/clients/dsh_client.py",
        "witty_agent_server/runtimes/openclaw_gateway_runtime.py",
        "witty_service/sandbox/e2b.py",
    ],
)
def test_checker_rejects_deleted_runtime_modules(tmp_path: Path, module: str) -> None:
    # 即使源码树里「存在」同名文件（例如误恢复），也必须被显式黑名单拦下。
    src = _make_src(tmp_path, [module])
    wheel = _write_wheel(tmp_path / "forbidden.whl", modules=[module])

    problems = checker.check_wheel(wheel, src)

    assert any("已删除的模块" in problem for problem in problems)


def test_checker_keeps_openclaw_skill_sync_module(tmp_path: Path) -> None:
    module = "witty_service/application/awesome_openclaw_sync.py"
    src = _make_src(tmp_path, [module])
    wheel = _write_wheel(tmp_path / "skill.whl", modules=[module], metadata_requires=[])

    assert checker.check_wheel(wheel, src) == []


@pytest.mark.parametrize(
    "requirement",
    [
        "deepseek-harness-sdk==0.2.1rc2",
        "deepseek_harness>=0.2",
        "deepseek.harness.sdk>=0.2",
        "deepseek__harness--sdk",
        "e2b-code-interpreter>=1.0",
        "e2b.code.interpreter>=1.0",
        "openclaw",
    ],
)
def test_checker_rejects_deleted_dependencies(tmp_path: Path, requirement: str) -> None:
    src = _make_src(tmp_path, [])
    wheel = _write_wheel(
        tmp_path / "deps.whl",
        modules=[],
        metadata_requires=[requirement],
    )

    problems = checker.check_wheel(wheel, src)

    assert any("已删除依赖" in problem for problem in problems)


def test_checker_ignores_non_package_directories(tmp_path: Path) -> None:
    # wheel 的 .data 目录与同前缀的模块目录不属于受管包，不得据此阻断发布。
    src = _make_src(tmp_path, [])
    wheel = _write_wheel(
        tmp_path / "data.whl",
        modules=[
            "witty_service-0.1.0.data/scripts/tool.py",
            "witty_service_extra/main.py",
            "witty_agent_server_backup/main.py",
        ],
        metadata_requires=[],
    )

    assert checker.check_wheel(wheel, src) == []


def test_checker_requires_metadata(tmp_path: Path) -> None:
    src = _make_src(tmp_path, [])
    wheel = _write_wheel(tmp_path / "no-metadata.whl", modules=[])

    problems = checker.check_wheel(wheel, src)

    assert any("缺少 METADATA" in problem for problem in problems)


def test_cli_exit_code_reflects_problems(tmp_path: Path) -> None:
    src = _make_src(tmp_path, [])
    bad = _write_wheel(tmp_path / "bad.whl", modules=["witty_service/sandbox/e2b.py"])

    assert checker.main([str(bad), "--src-root", str(src)]) == 1


def test_cli_exit_code_zero_on_success(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["witty_service/main.py"])
    good = _write_wheel(
        tmp_path / "good.whl", modules=["witty_service/main.py"], metadata_requires=[]
    )

    assert checker.main([str(good), "--src-root", str(src)]) == 0

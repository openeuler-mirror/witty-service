from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from witty_service.application.cve_service import CveService


@pytest.fixture
def service(tmp_path: Path, monkeypatch) -> CveService:
    monkeypatch.setenv("HOME", str(tmp_path))
    services = Mock()
    services.workspace_store.base_dir = tmp_path / "workspace"
    return CveService(services)


def _log_artifact(result: dict) -> dict:
    return next(
        item
        for item in result["branches"][0]["artifacts"]
        if item["kind"] == "backport_log"
    )


def test_get_workbench_does_not_expose_unrelated_global_portgpt_logs(
    service: CveService, tmp_path: Path
) -> None:
    """未关联 task/run/artifact 时，不能回退读取全局 PortGPT 日志。"""
    portgpt_dir = tmp_path / ".patchflow" / "logs" / "portgpt"
    portgpt_dir.mkdir(parents=True)
    (portgpt_dir / "patchflow-20260810-120000-a1b2c3d4-all.log").write_text(
        "other CVE log\n", encoding="utf-8"
    )

    artifact = _log_artifact(service.get_workbench("CVE-2024-1234", "OLK-6.6"))

    assert artifact["path"] == ""
    assert artifact["status"] == "missing"
    assert artifact["viewable"] is False


def test_get_workbench_without_logs(service: CveService) -> None:
    """无任何日志时回退为 missing。"""
    artifact = _log_artifact(service.get_workbench("CVE-2024-1234", "OLK-6.6"))

    assert artifact["path"] == ""
    assert artifact["status"] == "missing"
    assert artifact["viewable"] is False


def test_get_workbench_reads_current_cvekit_cache_key_and_conflict_patch(
    service: CveService, tmp_path: Path
) -> None:
    """兼容 cvekit 当前的 CVE|branches|repo_path 缓存 key。"""
    clone_dir = tmp_path / "Image"
    repo_dir = clone_dir / "kernel"
    repo_dir.mkdir(parents=True)
    source_patch = clone_dir / f"commit_patch_{'a' * 40}.patch"
    source_patch.write_text("patch\n", encoding="utf-8")

    cve_id = "CVE-2026-64563"
    branches = "OLK-6.6,OLK-5.10"
    sorted_branches = "OLK-5.10,OLK-6.6"
    cache_key = hashlib.md5(
        f"{cve_id}|{sorted_branches}|{repo_dir}".encode(), usedforsecurity=False
    ).hexdigest()
    cache_dir = tmp_path / ".cve_analyzer_cache"
    cache_dir.mkdir()
    (cache_dir / "branches_analysis_cache.json").write_text(
        json.dumps(
            {
                cache_key: {
                    "data": [
                        {
                            "Target branch": "OLK-5.10",
                            "Adaptation status": "success",
                            "Suggested adjustment files": "A",
                            "Conflict point": str(source_patch),
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = service.get_workbench(cve_id, branches, str(clone_dir))
    branch = next(item for item in result["branches"] if item["name"] == "OLK-5.10")
    original_patch = next(
        item for item in branch["artifacts"] if item["kind"] == "original_patch"
    )
    backport_patch = next(
        item for item in branch["artifacts"] if item["kind"] == "backport_patch"
    )

    assert result["cache_key"] == cache_key
    assert original_patch["path"] == str(source_patch)
    assert original_patch["viewable"] is True
    assert backport_patch["status"] == "无需回移植"


def test_get_pr_readiness_accepts_cvekit_hashed_fix_branch(
    service: CveService, tmp_path: Path, monkeypatch
) -> None:
    """cvekit 自动追加 repo_path hash 的修复分支也应当可提交 PR。"""
    clone_dir = tmp_path / "Image"
    repo_dir = clone_dir / "kernel"
    (repo_dir / ".git").mkdir(parents=True)
    path_hash = hashlib.md5(str(repo_dir).encode()).hexdigest()[:6]
    fix_branch = f"fix-OLK-5.10-16867-{path_hash}"
    monkeypatch.setattr(
        "witty_service.application.cve_service.subprocess.run",
        lambda *args, **kwargs: Mock(stdout=f"* {fix_branch} abc123 test\n"),
    )

    result = service.get_pr_readiness(
        "CVE-2026-64563", "OLK-5.10", str(clone_dir), "16867"
    )

    assert result["ready"] is True
    assert result["branches"] == [
        {
            "branch": "OLK-5.10",
            "fix_branch": fix_branch,
            "ready": True,
            "reason": "local_fix_branch_exists",
        }
    ]

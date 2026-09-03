"""src/witty_service/application/backport_cvekit_client.py 的单元测试。

策略:
- mock cvekit 二进制 + BackportGitClient,把执行路径短路
- 覆盖:静态工具 / LLM 参数合并 / report 读写对齐 / 状态判定 / 8 个高级流程
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from witty_service.application.backport_cvekit_client import (
    BackportCvekitClient,
    BackportRuntimeConfig,
    CvekitCommandError,
)

# ── 夹具 & 工具 ──────────────────────────────────────────────────


@pytest.fixture()
def fake_cvekit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    bin_path = bin_dir / "cvekit"
    bin_path.write_text("#!/bin/sh\n")
    bin_path.chmod(0o755)
    monkeypatch.setattr(
        BackportCvekitClient, "resolve_cvekit_path", staticmethod(lambda: bin_path)
    )
    return bin_path


@pytest.fixture()
def client(tmp_path: Path, fake_cvekit: Path) -> BackportCvekitClient:
    client = BackportCvekitClient(runs_root=tmp_path / "runs")
    client.set_runtime_config(_runtime_config())
    return client


def _runtime_config(
    *,
    backport_engine: str = "mystique",
    format_mode: str = "changed",
) -> BackportRuntimeConfig:
    return BackportRuntimeConfig(
        llm_provider="openai",
        api_key="sk-test",
        llm_base_url="https://api.example.com",
        llm_model_name="gpt-4",
        backport_engine=backport_engine,
        format_mode=format_mode,
    )


def _write_report(path: Path, *, commits: list[dict], **extra: Any) -> None:
    path.write_text(
        yaml.safe_dump(
            {"commits": commits, **extra}, allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def _archive_task(client: BackportCvekitClient, tmp_path: Path) -> Path:
    """在 client 的归档根下创建真实 Task 目录（含 task.json / input / runs / cases）。"""
    excel = tmp_path / "seed.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    return client._run_store.create_task(
        excel_path=excel,
        target_repository="/repo/kernel",
    )


# ── 静态 / 类方法工具 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd,expected",
    [
        (["a", "b"], ["a", "b"]),
        (["--api-key", "sk-x"], ["--api-key", "***"]),
        (["--api-key=sk-x"], ["--api-key=***"]),
    ],
)
def test_redact_command(cmd: list[str], expected: list[str]) -> None:
    assert BackportCvekitClient._redact_command(cmd) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a":1}', {"a": 1}),
        ('noise {"a":1} tail', {"a": 1}),
        ("", {}),
        ("garbage", {}),
        ("[]", {}),
    ],
)
def test_parse_json_output(raw: str, expected: dict) -> None:
    assert BackportCvekitClient._parse_json_output(raw) == expected


def test_run_cvekit_preserves_structured_lock_error(
    client: BackportCvekitClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "status": "failed",
        "message": "TARGET_REPOSITORY_LOCK_TIMEOUT: busy with sk-test",
        "error_code": "TARGET_REPOSITORY_LOCK_TIMEOUT",
        "retryable": True,
        "wait_seconds": 2.1,
        "timeout_seconds": 2,
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    with pytest.raises(CvekitCommandError) as raised:
        client._run_cvekit(["--action", "backport-batch", "--json"], tmp_path)

    assert raised.value.error_code == "TARGET_REPOSITORY_LOCK_TIMEOUT"
    assert raised.value.retryable is True
    assert raised.value.wait_seconds == 2.1
    assert raised.value.timeout_seconds == 2
    assert "sk-test" not in str(raised.value)


def test_build_env_forwards_only_configured_lock_settings(
    client: BackportCvekitClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_settings = {
        "CVEKIT_LOCK_DIR": "/tmp/cvekit-locks",
        "CVEKIT_CACHE_LOCK_TIMEOUT": "30",
        "CVEKIT_REPOSITORY_LOCK_TIMEOUT": "900",
        "CVEKIT_LOCK_POLL_INTERVAL": "0.5",
        "CVEKIT_LOCK_LOG_INTERVAL": "10",
    }
    for key in lock_settings:
        monkeypatch.delenv(key, raising=False)
    assert all(key not in client._build_env() for key in lock_settings)

    for key, value in lock_settings.items():
        monkeypatch.setenv(key, value)

    env = client._build_env()

    assert {key: env[key] for key in lock_settings} == lock_settings


def test_run_cvekit_keeps_legacy_error_for_unstructured_output(
    client: BackportCvekitClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="legacy failure",
            stderr="details",
        ),
    )

    with pytest.raises(RuntimeError, match="cvekit 执行失败") as raised:
        client._run_cvekit(["--action", "backport-batch"], tmp_path)

    assert not isinstance(raised.value, CvekitCommandError)


def test_read_report_ok(tmp_path: Path) -> None:
    p = tmp_path / "r.yml"
    _write_report(p, commits=[{"a": 1}])
    data, commits = BackportCvekitClient._read_report(p)
    assert data["commits"] == [{"a": 1}]
    assert commits == [{"a": 1}]


def test_read_report_invalid_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "r.yml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        BackportCvekitClient._read_report(p)


def test_build_patch_meta(tmp_path: Path) -> None:
    real = tmp_path / "x.patch"
    real.write_text("x")
    item = {
        "original_patch_path": str(real),
        "patch_path": "/missing",
        "backported_patch_path": "",
    }
    meta = BackportCvekitClient._build_patch_meta(item)
    assert (
        meta["original"]["exists"] is True
        and meta["original"]["file_name"] == "x.patch"
    )
    assert meta["current"]["exists"] is False
    assert meta["backported"]["file_name"] == ""


def test_sanitize_commit_item_strips_patch_paths() -> None:
    item = {
        "original_patch_path": "/a",
        "patch_path": "/b",
        "backported_patch_path": "/c",
        "commit": "abc",
    }
    out = BackportCvekitClient.sanitize_commit_item(item)
    for k in BackportCvekitClient.PATCH_KEYS:
        assert k not in out
    assert out["row_id"] == "abc"
    assert "original" in out["patches"]


def test_sanitize_commit_list_filters_non_dict() -> None:
    out = BackportCvekitClient.sanitize_commit_list([{"a": 1}, "x", None, {"b": 2}])
    assert len(out) == 2


def test_overlay_commit_skips_protected_keys() -> None:
    raw = {"a": 1, "b": 2}
    merged = BackportCvekitClient._overlay_commit(
        raw, {"a": 9, "row_id": "x", "patches": "y", "patch_path": "z"}
    )
    assert merged["a"] == 9 and merged["b"] == 2
    assert "row_id" not in merged and "patches" not in merged


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"commit": "c1"}, "c1"),
        ({"original_patch_path": "/p"}, "/p"),
        ({"patch_path": "/q"}, "/q"),
        ({"unrelated": True}, json.dumps({"unrelated": True}, sort_keys=True)),
    ],
)
def test_build_row_id_priority(row: dict, expected: str) -> None:
    assert BackportCvekitClient._build_row_id(row) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("auto", "auto"),
        ("openEuler", "openEuler"),
        ("upstream", "upstream"),
        ("", "upstream"),
        ("unknown", "upstream"),
    ],
)
def test_normalize_commit_message_source(value: str, expected: str) -> None:
    assert BackportCvekitClient._normalize_commit_message_source(value) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("missing prerequisite X", True),
        ("Patch does not apply", True),
        ("Some other error", False),
    ],
)
def test_infer_likely_missing_prerequisite(text: str, expected: bool) -> None:
    assert BackportCvekitClient._infer_likely_missing_prerequisite(text) is expected


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"status": "skipped"}, True),
        ({"is_merge_commit": True}, True),
        ({"status": "success"}, False),
        ({}, False),
    ],
)
def test_is_skipped_row(row: dict, expected: bool) -> None:
    assert BackportCvekitClient._is_skipped_row(row) is expected


def test_is_blocking_conflict_distinguishes_skipped() -> None:
    assert (
        BackportCvekitClient._is_blocking_conflict(
            {"has_conflict": True, "status": "failed"}
        )
        is False
    )
    assert (
        BackportCvekitClient._is_blocking_conflict(
            {"has_conflict": True, "status": "skipped"}
        )
        is False
    )
    assert BackportCvekitClient._is_blocking_conflict({"has_conflict": False}) is False


def test_is_pending_row() -> None:
    assert BackportCvekitClient._is_pending_row({"status": "pending"}) is True
    assert BackportCvekitClient._is_pending_row({}) is False


def test_merge_report_rows_overlays_by_row_id() -> None:
    a = {"row_id": "1", "v": "old"}
    new = [{"row_id": "1", "v": "new"}, {"row_id": "3", "v": "y"}]
    out = BackportCvekitClient._merge_report_rows([a, {"row_id": "2"}], new)
    assert out[0]["v"] == "new"


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"backported_patch_path": "b"}, "b"),
        ({"patch_path": "p"}, "p"),
        ({"original_patch_path": "o"}, "o"),
    ],
)
def test_resolve_apply_value_priority(row: dict, expected: str) -> None:
    assert BackportCvekitClient._resolve_apply_value(row) == expected


def test_resolve_apply_value_no_field_raises() -> None:
    with pytest.raises(ValueError):
        BackportCvekitClient._resolve_apply_value({})


@pytest.mark.parametrize(
    "row,expected_status",
    [
        ({"status": "success", "applied_commit": "abc"}, True),
        (
            {"status": "success", "backported_patch_path": "/p", "applied_commit": ""},
            True,
        ),
        ({"status": "error", "backported_patch_path": "/p"}, False),
    ],
)
def test_normalize_try_resolve_row(row: dict, expected_status: bool) -> None:
    out = BackportCvekitClient._normalize_try_resolve_row(row)
    if expected_status:
        assert out.get("has_conflict") is False


# ── 初始化 & 路径解析 ────────────────────────────────────────────


def test_init_creates_runs_root(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    c = BackportCvekitClient(runs_root=runs)
    assert c._runs_root == runs.expanduser().resolve()
    assert not hasattr(c, "_openclaw_config_path")


def test_init_does_not_read_openclaw_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_called() -> None:
        raise AssertionError("BackportCvekitClient should not read global settings")

    monkeypatch.setattr("witty_service.config.get_settings", fail_if_called)
    c = BackportCvekitClient(runs_root=tmp_path / "runs")
    assert c._runs_root == (tmp_path / "runs").resolve()


def test_resolve_cvekit_path_via_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "cvekit"
    fake_bin.write_text("")
    fake_bin.chmod(0o755)
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=str(fake_bin) + "\n", stderr=""
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_completed)
    assert BackportCvekitClient.resolve_cvekit_path() == fake_bin.resolve()


def test_resolve_cvekit_path_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=""
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_completed)
    with pytest.raises(RuntimeError):
        BackportCvekitClient.resolve_cvekit_path()


# ── LLM 配置 ────────────────────────────────────────────────────


def test_build_env_includes_joern_from_process_env(
    monkeypatch: pytest.MonkeyPatch, client: BackportCvekitClient
) -> None:
    monkeypatch.setenv("JOERN_PATH", "/opt/joern")
    env = client._build_env()
    assert env["JOERN_PATH"] == "/opt/joern"


def test_build_env_uses_isolated_patchflow_log_root(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    env = client._build_env()

    assert env["CVEKIT_LOG_DIR"] == str(tmp_path / ".patchflow" / "logs")


# ── cvekit 执行 ───────────────────────────────────────────────────


def test_run_cvekit_success(
    client: BackportCvekitClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = client._run_cvekit(["--action", "test"], cwd=client._runs_root)
    assert out.stdout == "ok"


def test_run_cvekit_failure_redacts(
    client: BackportCvekitClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="oops", stderr="bad"
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    with pytest.raises(RuntimeError) as exc:
        client._run_cvekit(["--api-key", "sk", "--action", "x"], cwd=client._runs_root)
    assert "sk" not in str(exc.value) and "***" in str(exc.value)


def test_run_cvekit_appends_llm_and_backport_options(
    client: BackportCvekitClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: (
            seen.update(cmd=cmd, env=kw.get("env"))
            or subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        ),
    )
    client._run_cvekit(["--action", "batch"], cwd=client._runs_root)
    cmd = seen["cmd"]
    env = seen["env"]
    assert "--api-key" not in cmd
    assert env["API_KEY"] == "sk-test"
    assert (
        "--llm-provider" in cmd
        and "--llm-base-url" in cmd
        and "--llm-model-name" in cmd
    )
    assert cmd[cmd.index("--backport-engine") + 1] == "mystique"
    assert cmd[cmd.index("--format-mode") + 1] == "changed"


def test_run_cvekit_does_not_overwrite_existing_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = BackportCvekitClient(runs_root=tmp_path / "runs")
    c.set_runtime_config(_runtime_config())
    monkeypatch.setattr(
        BackportCvekitClient,
        "resolve_cvekit_path",
        staticmethod(lambda: Path("/bin/cvekit")),
    )
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: (
            seen.update(cmd=cmd)
            or subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        ),
    )
    c._run_cvekit(["--llm-provider", "override", "--action", "x"], cwd=tmp_path)
    assert seen["cmd"].index("--llm-provider") < seen["cmd"].index("override")
    assert seen["cmd"].count("--llm-provider") == 1


# ── Report 对齐 & 写盘 ──────────────────────────────────────────


def test_resolve_commit_row_in_base(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    base = tmp_path / "base.yml"
    _write_report(base, commits=[{"row_id": "1", "v": "B"}])
    out = client._resolve_commit_row(row={"row_id": "1"}, base_report_path=str(base))
    assert out["v"] == "B"


def test_resolve_commit_row_prefers_working(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    base = tmp_path / "base.yml"
    work = tmp_path / "work.yml"
    _write_report(base, commits=[{"row_id": "1", "v": "B"}])
    _write_report(work, commits=[{"row_id": "1", "v": "W"}])
    out = client._resolve_commit_row(
        row={"row_id": "1"}, base_report_path=str(base), working_report_path=str(work)
    )
    assert out["v"] == "W"


def test_resolve_commit_row_not_found_raises(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    base = tmp_path / "base.yml"
    _write_report(base, commits=[{"row_id": "1"}])
    with pytest.raises(ValueError):
        client._resolve_commit_row(row={"row_id": "999"}, base_report_path=str(base))


def test_resolve_commit_row_missing_files_raises(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        client._resolve_commit_row(
            row={"row_id": "x"},
            base_report_path=str(tmp_path / "absent.yml"),
            working_report_path=str(tmp_path / "absent2.yml"),
        )


def test_mark_merged_by_subject_applies_match(
    client: BackportCvekitClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "witty_service.application.backport_cvekit_client.BackportGitClient.collect_subject_map",
        staticmethod(lambda target: {"fix foo": "abc123"}),
    )
    report = {
        "commits": [
            {"commit_title": "fix foo", "merged_in_target": False, "has_conflict": True}
        ]
    }
    n = client._mark_merged_by_subject(report, {"fix foo": "abc123"})
    assert n == 1 and report["commits"][0]["merged_in_target"] is True


def test_mark_merged_by_subject_no_match_or_empty(client: BackportCvekitClient) -> None:
    assert client._mark_merged_by_subject({"commits": [{"commit_title": "x"}]}, {}) == 0
    assert client._mark_merged_by_subject({}, {"a": "b"}) == 0
    assert client._mark_merged_by_subject({"commits": "not-list"}, {"a": "b"}) == 0


def test_reconcile_report_empty_subject_map(
    client: BackportCvekitClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "witty_service.application.backport_cvekit_client.BackportGitClient.collect_subject_map",
        staticmethod(lambda target: {}),
    )
    report = {"commits": [{"commit_title": "x"}]}
    assert client._reconcile_report(report, "/tmp") is report


def test_write_refresh_meta_with_and_without_fallback(
    client: BackportCvekitClient,
) -> None:
    report: dict[str, Any] = {}
    state = {
        "target_path": "/r",
        "target_branch": "main",
        "target_head": "abc",
        "target_status_clean": True,
    }
    client._write_refresh_meta(
        report, state, mode="x", checked_count=1, skipped_count=0
    )
    assert report["refresh_meta"]["target_path"] == "/r"
    assert report["refresh_meta"]["refresh_mode"] == "x"
    report.clear()
    client._write_refresh_meta(
        report, {}, mode="x", checked_count=0, skipped_count=5, fallback_reason="no map"
    )
    assert report["refresh_meta"]["fallback_reason"] == "no map"


def test_write_report_config_strips_api_key(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yml"
    BackportCvekitClient._write_report_config(
        p, {"api_key": "sk", "keep": 1}, [{"a": 1}]
    )
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "api_key" not in data and data["keep"] == 1 and data["commits"] == [{"a": 1}]


def test_run_stop_at_first_conflict_report_creates_dir_and_invokes(
    client: BackportCvekitClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: dict[str, Any] = {}

    def fake_run(self, args, cwd, **kwargs):
        called["args"] = args
        cfg = next(a for a in args if a.endswith(".report.yml"))
        Path(cfg).write_text(
            yaml.safe_dump({"commits": [{"row_id": "1", "v": "after"}]}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
    run_dir, _, commits = client._run_stop_at_first_conflict_report(
        report_data={"k": 1}, commits=[{"row_id": "1"}], run_prefix="t"
    )
    assert (
        run_dir.exists()
        and called["args"][1] == "backport-batch"
        and commits[0]["v"] == "after"
    )


def test_check_row_cleans_temp_run_dir(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _archive_task(client, tmp_path) / "b.yml"
    _write_report(
        base,
        commits=[{"row_id": "1", "status": "failed", "patch_path": "/tmp/1.patch"}],
    )
    called: dict[str, Any] = {}

    def fake_run(self, args, cwd, **kwargs):
        called["run_dir"] = Path(cwd)
        cfg = next(a for a in args if a.endswith(".report.yml"))
        Path(cfg).write_text(
            yaml.safe_dump({"commits": [{"row_id": "1", "status": "success"}]}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
    out = client.check_row(base_report_path=str(base), row={"row_id": "1"})

    assert out["status"] == "success"
    assert out["artifacts"]["base_report_path"] == str(base.resolve())
    assert called["run_dir"].name.startswith("witty-backport-")
    assert not called["run_dir"].exists()

    _, commits = BackportCvekitClient._read_report(base)
    assert commits[0]["status"] == "success"


# ── generate_report ───────────────────────────────────────────────


def test_generate_report_flow(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    excel = tmp_path / "in.xlsx"
    excel.write_text("x")
    target = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        "witty_service.application.backport_cvekit_client.BackportGitClient.ensure_git_repo",
        staticmethod(lambda p: None),
    )
    monkeypatch.setattr(
        "witty_service.application.backport_cvekit_client.BackportGitClient.get_repo_state",
        staticmethod(
            lambda p: {
                "target_path": str(target),
                "target_branch": "main",
                "target_head": "h",
                "target_status_clean": True,
            }
        ),
    )
    monkeypatch.setattr(
        "witty_service.application.backport_cvekit_client.BackportGitClient.collect_subject_map",
        staticmethod(lambda p: {}),
    )

    def fake_run(self, args, cwd, **kwargs):
        if "--stop-at-first-conflict" in args:
            for a in args:
                if a.endswith("backport-batch.yml"):
                    Path(a + ".report.yml").write_text(
                        yaml.safe_dump({"commits": [{"row_id": "1", "v": "ok"}]}),
                        encoding="utf-8",
                    )
                    break
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
    out = client.generate_report(
        excel_path=str(excel),
        project_url="u",
        project_dir="d",
        source_branch="s",
        target_path=str(target),
        target_release="r",
        patch_dataset_dir="pd",
        signer_name="n",
        signer_email="e",
        commit_message_template="",
        commit_message_source="auto",
        linux_repo_path="lr",
    )
    assert out["status"] == "success" and out["report"]["commit_count"] == 1
    assert out["artifacts"]["report_path"].endswith("initial-report.yml")


def test_generate_report_excel_missing(client: BackportCvekitClient) -> None:
    with pytest.raises(FileNotFoundError):
        client.generate_report(
            excel_path="/no/such.xlsx",
            project_url="",
            project_dir="",
            source_branch="",
            target_path="/tmp",
            target_release="",
            patch_dataset_dir="",
            signer_name="",
            signer_email="",
            commit_message_template="",
            commit_message_source="auto",
            linux_repo_path="",
        )


def test_generate_report_from_commit_entries_archives_csv_and_raw_config(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _git_repo(tmp_path / "repo")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "witty_service.application.backport_cvekit_client.BackportGitClient.ensure_git_repo",
        staticmethod(lambda _path: None),
    )
    monkeypatch.setattr(
        "witty_service.application.backport_cvekit_client.BackportGitClient.get_repo_state",
        staticmethod(
            lambda _path: {
                "target_path": str(target),
                "target_branch": "main",
                "target_head": "h",
                "target_status_clean": True,
            }
        ),
    )
    monkeypatch.setattr(
        "witty_service.application.backport_cvekit_client.BackportGitClient.collect_subject_map",
        staticmethod(lambda _path: {}),
    )

    def fake_run(self, args, cwd, **kwargs):
        del self, cwd, kwargs
        calls.append(args)
        if "--stop-at-first-conflict" in args:
            raw_config = Path(args[args.index("--backport-config") + 1])
            _write_report(
                Path(f"{raw_config}.report.yml"),
                commits=[{"row_id": "1", "commit": "abcdef1"}],
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
    out = client.generate_report(
        excel_path=None,
        commit_entries=[{"commit": "abcdef1", "commit_title": "first title"}],
        prerequisite_commits=[
            {
                "commit": "abcdef2",
                "title": "required first",
                "required_by": ["abcdef1"],
            }
        ],
        project_url="u",
        project_dir="d",
        source_branch="s",
        target_path=str(target),
        target_release="r",
        patch_dataset_dir="pd",
        signer_name="n",
        signer_email="e",
        commit_message_template="",
        commit_message_source="auto",
        linux_repo_path="lr",
    )

    task_dir = Path(out["artifacts"]["run_dir"])
    assert all("--backport-excel" not in args for args in calls)
    assert (task_dir / "input" / "commits.csv").is_file()
    raw_config = yaml.safe_load(
        (task_dir / "input" / "backport-batch.yml").read_text(encoding="utf-8")
    )
    assert raw_config["commits"] == [
        {"commit": "abcdef1", "commit_title": "first title"},
        {
            "commit": "abcdef2",
            "commit_title": "required first",
            "origin": "prerequisite",
            "required_by": ["abcdef1"],
            "capabilities": [],
        },
    ]


# ── continue_report ───────────────────────────────────────────────


def test_continue_report_blocking_short_circuit(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    base = _archive_task(client, tmp_path) / "b.yml"
    _write_report(
        base, commits=[{"row_id": "1", "has_conflict": True, "status": "pending"}]
    )
    out = client.continue_report(base_report_path=str(base))
    assert out["status"] == "failed" and "阻塞冲突" in out["summary"]


def test_continue_report_no_pending_short_circuit(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    base = _archive_task(client, tmp_path) / "b.yml"
    _write_report(base, commits=[{"row_id": "1", "status": "success"}])
    out = client.continue_report(base_report_path=str(base))
    assert out["status"] == "success"


def test_continue_report_runs_cvekit_when_pending(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _archive_task(client, tmp_path) / "b.yml"
    _write_report(
        base,
        commits=[
            {"row_id": "1", "status": "success"},
            {"row_id": "2", "status": "pending"},
        ],
    )

    def fake_run(self, args, cwd, **kwargs):
        cfg = next(a for a in args if a.endswith(".report.yml"))
        Path(cfg).write_text(
            yaml.safe_dump(
                {
                    "commits": [
                        {"row_id": "1", "status": "success"},
                        {"row_id": "2", "status": "success"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
    out = client.continue_report(base_report_path=str(base))
    assert out["status"] == "success" and "pending" in out["summary"]


def test_continue_report_file_missing_or_empty(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        client.continue_report(base_report_path=str(tmp_path / "absent.yml"))
    _write_report(tmp_path / "b.yml", commits=[])
    with pytest.raises(RuntimeError):
        client.continue_report(base_report_path=str(tmp_path / "b.yml"))


# ── recheck_conflict ─────────────────────────────────────────────


def test_recheck_conflict_no_blocking_or_wrong_row(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "b.yml"
    _write_report(base, commits=[{"row_id": "1", "status": "success"}])
    assert (
        client.recheck_conflict(base_report_path=str(base), row={"row_id": "1"})[
            "status"
        ]
        == "failed"
    )

    work = tmp_path / "w.yml"
    _write_report(
        base, commits=[{"row_id": "1", "has_conflict": True, "status": "pending"}]
    )
    _write_report(work, commits=[{"row_id": "999", "v": "x"}])
    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", lambda *a, **kw: None)
    out = client.recheck_conflict(
        base_report_path=str(base), row={"row_id": "999"}, working_report_path=str(work)
    )
    assert out["status"] == "failed" and "只能检测" in out["summary"]


def test_recheck_conflict_success(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _archive_task(client, tmp_path) / "b.yml"
    _write_report(
        base, commits=[{"row_id": "1", "has_conflict": True, "status": "pending"}]
    )

    def fake_run(self, args, cwd, **kwargs):
        cfg = next(a for a in args if a.endswith(".report.yml"))
        Path(cfg).write_text(
            yaml.safe_dump({"commits": [{"row_id": "1", "status": "success"}]}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
    out = client.recheck_conflict(base_report_path=str(base), row={"row_id": "1"})
    assert (
        out["status"] == "success"
        and out["report"]["commits"][0]["status"] == "success"
    )


# ── execute_selected / try_resolve ───────────────────────────────


def test_execute_selected_no_actionable(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    base = _archive_task(client, tmp_path) / "b.yml"
    _write_report(base, commits=[{"row_id": "1", "merged_in_target": True}])
    out = client.execute_selected(
        base_report_path=str(base),
        selected_commits=[{"row_id": "1"}],
        target_path="",
        patch_dataset_dir="",
        signer_name="",
        signer_email="",
        commit_message_template="",
        commit_message_source="auto",
        linux_repo_path="",
    )
    assert out["status"] == "success" and "无需执行" in out["summary"]


def test_execute_selected_empty_or_unresolvable_raises(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    base = tmp_path / "b.yml"
    _write_report(base, commits=[{"row_id": "1"}])
    with pytest.raises(ValueError):
        client.execute_selected(
            base_report_path=str(base),
            selected_commits=[],
            target_path="",
            patch_dataset_dir="",
            signer_name="",
            signer_email="",
            commit_message_template="",
            commit_message_source="auto",
            linux_repo_path="",
        )
    with pytest.raises(ValueError):
        client.execute_selected(
            base_report_path=str(base),
            selected_commits=[{"row_id": "999"}],
            target_path="",
            patch_dataset_dir="",
            signer_name="",
            signer_email="",
            commit_message_template="",
            commit_message_source="auto",
            linux_repo_path="",
        )


def test_execute_selected_success_and_diagnose(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _archive_task(client, tmp_path)
    client.set_archive_run_id(task_dir.name)
    client._run_store.update_manifest(task_dir, {"current_run": 1})
    base = task_dir / "b.yml"

    def run_ok(self, args, cwd, **kwargs):
        config_path = args[args.index("--backport-config") + 1]
        assert "-e" in args
        assert config_path.endswith(".report.yml")
        cfg = next(a for a in args if a.endswith(".yml"))
        Path(cfg).write_text(
            yaml.safe_dump({"commits": [{"row_id": "1", "status": "success"}]}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="ok", stderr=""
        )

    def run_diag(self, args, cwd, **kwargs):
        cfg = next(a for a in args if a.endswith(".yml"))
        Path(cfg).write_text(
            yaml.safe_dump({"commits": [{"row_id": "1", "status": "failed"}]}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="Patch does not apply",
            stderr="missing prerequisite X",
        )

    _write_report(
        base, commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}]
    )
    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", run_ok)
    out_ok = client.execute_selected(
        base_report_path=str(base),
        selected_commits=[{"row_id": "1"}],
        target_path="/t",
        patch_dataset_dir="pd",
        signer_name="n",
        signer_email="e",
        commit_message_template="tpl",
        commit_message_source="openEuler",
        linux_repo_path="lr",
    )
    assert (
        out_ok["status"] == "success"
        and out_ok["diagnostics"]["likely_missing_prerequisite"] is False
    )

    _write_report(
        base, commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}]
    )
    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", run_diag)
    out_diag = client.execute_selected(
        base_report_path=str(base),
        selected_commits=[{"row_id": "1"}],
        target_path="",
        patch_dataset_dir="",
        signer_name="",
        signer_email="",
        commit_message_template="",
        commit_message_source="auto",
        linux_repo_path="",
    )
    assert out_diag["diagnostics"]["likely_missing_prerequisite"] is True


def test_execute_selected_without_archive_run_uses_interaction(
    client: BackportCvekitClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    excel = tmp_path / "input.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    task_dir = client._run_store.create_task(
        excel_path=excel,
        target_repository="/repo/kernel",
    )
    base = task_dir / "input" / "initial-report.yml"
    row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "d" * 40,
        "status": "pending",
        "patch_path": "/missing/original.patch",
    }
    _write_report(base, commits=[row])
    client._run_store.update_manifest(
        task_dir,
        {
            "status": "ready",
            "current_run": None,
            "current_report": "input/initial-report.yml",
        },
    )
    client._run_store.set_run_id(None)

    def fake_run(args, cwd, **kwargs):
        config_path = Path(args[args.index("--backport-config") + 1])
        _write_report(
            config_path,
            commits=[{**row, "status": "success"}],
        )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="ok",
            stderr="",
        )

    monkeypatch.setattr(client, "_run_cvekit", fake_run)

    result = client.execute_selected(
        base_report_path=str(base),
        selected_commits=[row],
        target_path="/repo/kernel",
        patch_dataset_dir="",
        signer_name="",
        signer_email="",
        commit_message_template="",
        commit_message_source="auto",
        linux_repo_path="",
    )

    archive = result["archive"]
    assert archive["scope"] == "interaction"
    assert archive["number"] == 1
    case_path = task_dir / "cases" / archive["case_id"] / "case.json"
    case = json.loads(case_path.read_text(encoding="utf-8"))
    assert case["last_interaction"] == 1
    assert case["last_run"] is None
    assert client._run_store.read_manifest(task_dir)["status"] == "ready"
    assert not Path(result["artifacts"]["attempt_dir"]).exists()


def test_try_resolve_no_blocking_or_wrong_row(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "b.yml"
    _write_report(base, commits=[{"row_id": "1", "status": "success"}])
    out = client.try_resolve(
        base_report_path=str(base),
        row={"row_id": "1"},
        target_path="",
        patch_dataset_dir="",
        signer_name="",
        signer_email="",
        commit_message_template="",
        commit_message_source="auto",
        linux_repo_path="",
    )
    assert out["status"] == "failed" and "没有可处理" in out["summary"]

    work = tmp_path / "w.yml"
    _write_report(
        base, commits=[{"row_id": "1", "has_conflict": True, "status": "pending"}]
    )
    _write_report(work, commits=[{"row_id": "999", "patch_path": "/p"}])
    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", lambda *a, **kw: None)
    out = client.try_resolve(
        base_report_path=str(base),
        row={"row_id": "999"},
        working_report_path=str(work),
        target_path="",
        patch_dataset_dir="",
        signer_name="",
        signer_email="",
        commit_message_template="",
        commit_message_source="auto",
        linux_repo_path="",
    )
    assert out["status"] == "failed" and "只能处理" in out["summary"]


def test_try_resolve_with_blocking(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _archive_task(client, tmp_path) / "b.yml"
    _write_report(
        base,
        commits=[
            {
                "row_id": "1",
                "has_conflict": True,
                "status": "pending",
                "patch_path": "/p",
            }
        ],
    )

    def fake_run(self, args, cwd, **kwargs):
        cfg = next(a for a in args if a.endswith(".yml"))
        Path(cfg).write_text(
            yaml.safe_dump(
                {
                    "commits": [
                        {"row_id": "1", "status": "success", "applied_commit": "abc"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
    out = client.try_resolve(
        base_report_path=str(base),
        row={"row_id": "1"},
        target_path="/t",
        patch_dataset_dir="",
        signer_name="",
        signer_email="",
        commit_message_template="",
        commit_message_source="auto",
        linux_repo_path="",
    )
    assert (
        out["status"] == "success"
        and out["report"]["commits"][0]["merged_in_target"] is True
    )


# ── apply_row / preview_commit_message / load_patch_preview ────


def test_apply_row_already_merged_short_circuit(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    base = tmp_path / "b.yml"
    _write_report(base, commits=[{"row_id": "1", "merged_in_target": True}])
    out = client.apply_row(
        base_report_path=str(base),
        row={"row_id": "1"},
        commit_message_template="",
        commit_message_source="auto",
        signer_name="",
        signer_email="",
        linux_repo_path="",
    )
    assert out["status"] == "success" and "无需执行" in out["summary"]


def test_apply_row_success_and_failure(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _archive_task(client, tmp_path)
    client.set_archive_run_id(task_dir.name)
    client._run_store.update_manifest(task_dir, {"current_run": 1})
    base = task_dir / "b.yml"
    _write_report(
        base, commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}]
    )

    def run_ok(self, args, cwd, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"status": "success", "error": None}),
            stderr="",
        )

    def run_fail(self, args, cwd, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"status": "failed", "error": "patch broken"}),
            stderr="",
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", run_ok)
    out_ok = client.apply_row(
        base_report_path=str(base),
        row={"row_id": "1"},
        commit_message_template="",
        commit_message_source="auto",
        signer_name="",
        signer_email="",
        linux_repo_path="",
    )
    assert out_ok["status"] == "success"

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", run_fail)
    out_fail = client.apply_row(
        base_report_path=str(base),
        row={"row_id": "1"},
        commit_message_template="",
        commit_message_source="auto",
        signer_name="",
        signer_email="",
        linux_repo_path="",
    )
    assert out_fail["status"] == "failed" and "patch broken" in out_fail["summary"]


def test_apply_row_uses_working_report_when_exists(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _archive_task(client, tmp_path)
    client.set_archive_run_id(task_dir.name)
    client._run_store.update_manifest(task_dir, {"current_run": 1})
    base = task_dir / "b.yml"
    work = tmp_path / "w.yml"
    _write_report(
        base, commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}]
    )
    _write_report(
        work, commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}]
    )
    seen: dict[str, Any] = {}

    def run_ok(self, args, cwd, **kwargs):
        config = Path(next(a for a in args if a.endswith(".yml")))
        seen.update(
            config=str(config),
            report=yaml.safe_load(config.read_text(encoding="utf-8")),
        )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"status": "success"}),
            stderr="",
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", run_ok)
    client.apply_row(
        base_report_path=str(base),
        row={"row_id": "1"},
        commit_message_template="",
        commit_message_source="auto",
        signer_name="",
        signer_email="",
        linux_repo_path="",
        working_report_path=str(work),
    )
    execution_report = Path(seen["config"])
    assert execution_report.name == "apply.report.yml"
    assert execution_report.parent != work.parent
    assert seen["report"]["commits"][0]["row_id"] == "1"


def test_preview_commit_message_success_and_failure(
    client: BackportCvekitClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "b.yml"
    _write_report(
        base, commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}]
    )
    payload = {
        "status": "success",
        "commit_message": "fix: foo",
        "commit_message_context": {"x": 1},
        "source_detection": {"s": "u"},
        "commit_message_warnings": ["w"],
    }

    def run_ok(self, args, cwd, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    def run_fail(self, args, cwd, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"status": "failed", "error": "no tpl"}),
            stderr="",
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", run_ok)
    out = client.preview_commit_message(
        base_report_path=str(base),
        row={"row_id": "1"},
        commit_message_template="tpl",
        commit_message_source="upstream",
        linux_repo_path="lr",
    )
    assert out["status"] == "success" and out["commit_message"]["message"] == "fix: foo"

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", run_fail)
    with pytest.raises(RuntimeError):
        client.preview_commit_message(
            base_report_path=str(base),
            row={"row_id": "1"},
            commit_message_template="",
            commit_message_source="auto",
            linux_repo_path="",
        )


def test_load_patch_preview_success_and_errors(
    client: BackportCvekitClient, tmp_path: Path
) -> None:
    base = tmp_path / "b.yml"
    patch = tmp_path / "p.patch"
    patch.write_text("--- a\n+++ b\n")
    _write_report(base, commits=[{"row_id": "1", "original_patch_path": str(patch)}])
    out = client.load_patch_preview(
        base_report_path=str(base), row={"row_id": "1"}, patch_kind="original"
    )
    assert (
        out["status"] == "success"
        and out["patch"]["file_name"] == "p.patch"
        and "--- a" in out["patch"]["patch_text"]
    )

    with pytest.raises(ValueError):
        client.load_patch_preview(
            base_report_path=str(base), row={"row_id": "1"}, patch_kind="bogus"
        )
    _write_report(base, commits=[{"row_id": "2", "original_patch_path": ""}])
    with pytest.raises(FileNotFoundError):
        client.load_patch_preview(
            base_report_path=str(base), row={"row_id": "2"}, patch_kind="original"
        )
    _write_report(
        base, commits=[{"row_id": "3", "original_patch_path": "/no/such.patch"}]
    )
    with pytest.raises(FileNotFoundError):
        client.load_patch_preview(
            base_report_path=str(base), row={"row_id": "3"}, patch_kind="original"
        )


def test_override_commit_message_config_updates(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yml"
    p.write_text(yaml.safe_dump({"a": 1, "b": 2}), encoding="utf-8")
    BackportCvekitClient._override_commit_message_config(
        p,
        commit_message_template="tpl {{x}}",
        commit_message_source="upstream",
        signer_name="n",
        signer_email="e",
        linux_repo_path="/lr",
    )
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["commit_message_template"] == "tpl {{x}}"
    assert data["commit_message_source"] == "upstream"
    assert data["signer_name"] == "n" and data["signer_email"] == "e"
    # linux_repo_path 只在 source=auto 时写入
    BackportCvekitClient._override_commit_message_config(
        p,
        commit_message_template="",
        commit_message_source="auto",
        signer_name="",
        signer_email="",
        linux_repo_path="/lr",
    )
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["linux_repo_path"] == "/lr"


# ── target_config_layout / target_config_layout_opts 测试 ──


class TestTargetConfigLayoutYaml:
    def test_generate_report_writes_layout_to_base_config(
        self,
        client: BackportCvekitClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """generate_report 在 base_config YAML 中写入 layout 字段"""
        excel = tmp_path / "in.xlsx"
        excel.write_text("x")
        target = _git_repo(tmp_path / "repo")
        monkeypatch.setattr(
            "witty_service.application.backport_cvekit_client.BackportGitClient.ensure_git_repo",
            staticmethod(lambda p: None),
        )
        monkeypatch.setattr(
            "witty_service.application.backport_cvekit_client.BackportGitClient.get_repo_state",
            staticmethod(
                lambda p: {
                    "target_path": str(target),
                    "target_branch": "main",
                    "target_head": "h",
                    "target_status_clean": True,
                }
            ),
        )
        monkeypatch.setattr(
            "witty_service.application.backport_cvekit_client.BackportGitClient.collect_subject_map",
            staticmethod(lambda p: {}),
        )

        captured_base_config: dict[str, Any] = {}

        def fake_run(self, args, cwd, **kwargs):
            # 捕获 base_config 写入
            base_cfg_path = cwd / "input" / "backport.base.yml"
            if base_cfg_path.exists():
                captured_base_config.update(
                    yaml.safe_load(base_cfg_path.read_text(encoding="utf-8")) or {}
                )
            if "--stop-at-first-conflict" in args:
                for a in args:
                    if a.endswith("backport-batch.yml"):
                        Path(a + ".report.yml").write_text(
                            yaml.safe_dump({"commits": [{"row_id": "1", "v": "ok"}]}),
                            encoding="utf-8",
                        )
                        break
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
        client.generate_report(
            excel_path=str(excel),
            project_url="u",
            project_dir="d",
            source_branch="s",
            target_path=str(target),
            target_release="r",
            patch_dataset_dir="pd",
            signer_name="n",
            signer_email="e",
            commit_message_template="",
            commit_message_source="auto",
            linux_repo_path="lr",
            target_config_layout="anolis",
            target_config_layout_opts={"default_level": "L2-OPTIONAL"},
        )
        assert captured_base_config.get("target_config_layout") == "anolis"
        assert captured_base_config.get("target_config_layout_opts") == {
            "default_level": "L2-OPTIONAL"
        }

    def test_generate_report_layout_none_omits_fields(
        self,
        client: BackportCvekitClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """layout=none 时 base_config YAML 不含 layout 字段"""
        excel = tmp_path / "in.xlsx"
        excel.write_text("x")
        target = _git_repo(tmp_path / "repo")
        monkeypatch.setattr(
            "witty_service.application.backport_cvekit_client.BackportGitClient.ensure_git_repo",
            staticmethod(lambda p: None),
        )
        monkeypatch.setattr(
            "witty_service.application.backport_cvekit_client.BackportGitClient.get_repo_state",
            staticmethod(
                lambda p: {
                    "target_path": str(target),
                    "target_branch": "main",
                    "target_head": "h",
                    "target_status_clean": True,
                }
            ),
        )
        monkeypatch.setattr(
            "witty_service.application.backport_cvekit_client.BackportGitClient.collect_subject_map",
            staticmethod(lambda p: {}),
        )

        captured_base_config: dict[str, Any] = {}

        def fake_run(self, args, cwd, **kwargs):
            base_cfg_path = cwd / "backport.base.yml"
            if base_cfg_path.exists():
                captured_base_config.update(
                    yaml.safe_load(base_cfg_path.read_text(encoding="utf-8")) or {}
                )
            if "--stop-at-first-conflict" in args:
                for a in args:
                    if a.endswith("backport-batch.yml"):
                        Path(a + ".report.yml").write_text(
                            yaml.safe_dump({"commits": [{"row_id": "1", "v": "ok"}]}),
                            encoding="utf-8",
                        )
                        break
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
        client.generate_report(
            excel_path=str(excel),
            project_url="u",
            project_dir="d",
            source_branch="s",
            target_path=str(target),
            target_release="r",
            patch_dataset_dir="pd",
            signer_name="n",
            signer_email="e",
            commit_message_template="",
            commit_message_source="auto",
            linux_repo_path="lr",
            target_config_layout="none",
            target_config_layout_opts={"default_level": "L1-RECOMMEND"},
        )
        assert "target_config_layout" not in captured_base_config
        assert "target_config_layout_opts" not in captured_base_config

    def test_execute_selected_writes_layout_to_filtered_config(
        self,
        client: BackportCvekitClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """execute_selected 在 filtered config YAML 中写入 layout 字段"""
        base = _archive_task(client, tmp_path) / "b.yml"
        _write_report(
            base, commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}]
        )

        captured_config: dict[str, Any] = {}

        def fake_run(self, args, cwd, **kwargs):
            for a in args:
                if a.endswith("report.report.yml"):
                    captured_config.update(
                        yaml.safe_load(Path(a).read_text(encoding="utf-8")) or {}
                    )
            cfg = next(a for a in args if a.endswith(".yml"))
            Path(cfg).write_text(
                yaml.safe_dump({"commits": [{"row_id": "1", "status": "success"}]}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="ok", stderr=""
            )

        monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
        client.execute_selected(
            base_report_path=str(base),
            selected_commits=[{"row_id": "1"}],
            target_path="/t",
            patch_dataset_dir="pd",
            signer_name="n",
            signer_email="e",
            commit_message_template="tpl",
            commit_message_source="openEuler",
            linux_repo_path="lr",
            target_config_layout="anolis",
            target_config_layout_opts={"default_level": "L0-MANDATORY"},
        )
        assert captured_config.get("target_config_layout") == "anolis"
        assert captured_config.get("target_config_layout_opts") == {
            "default_level": "L0-MANDATORY"
        }

    def test_execute_selected_layout_none_omits_fields(
        self,
        client: BackportCvekitClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """execute_selected layout=none 时 filtered config 不含 layout 字段"""
        base = _archive_task(client, tmp_path) / "b.yml"
        _write_report(
            base, commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}]
        )

        captured_config: dict[str, Any] = {}

        def fake_run(self, args, cwd, **kwargs):
            for a in args:
                if a.endswith(".yml") and "filtered" in a:
                    captured_config.update(
                        yaml.safe_load(Path(a).read_text(encoding="utf-8")) or {}
                    )
            cfg = next(a for a in args if a.endswith(".yml"))
            Path(cfg).write_text(
                yaml.safe_dump({"commits": [{"row_id": "1", "status": "success"}]}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="ok", stderr=""
            )

        monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
        client.execute_selected(
            base_report_path=str(base),
            selected_commits=[{"row_id": "1"}],
            target_path="/t",
            patch_dataset_dir="pd",
            signer_name="n",
            signer_email="e",
            commit_message_template="tpl",
            commit_message_source="openEuler",
            linux_repo_path="lr",
            target_config_layout="none",
            target_config_layout_opts=None,
        )
        assert "target_config_layout" not in captured_config
        assert "target_config_layout_opts" not in captured_config

    def test_execute_selected_strips_old_layout_when_current_is_none(
        self,
        client: BackportCvekitClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """原 report 有 layout，当前传 none 时 filtered config 不含 layout 字段"""
        base = _archive_task(client, tmp_path) / "b.yml"
        _write_report(
            base,
            commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}],
            target_config_layout="anolis",
            target_config_layout_opts={"default_level": "L2-OPTIONAL"},
        )

        captured_config: dict[str, Any] = {}

        def fake_run(self, args, cwd, **kwargs):
            for a in args:
                if a.endswith(".yml") and "filtered" in a:
                    captured_config.update(
                        yaml.safe_load(Path(a).read_text(encoding="utf-8")) or {}
                    )
            cfg = next(a for a in args if a.endswith(".yml"))
            Path(cfg).write_text(
                yaml.safe_dump({"commits": [{"row_id": "1", "status": "success"}]}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="ok", stderr=""
            )

        monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
        client.execute_selected(
            base_report_path=str(base),
            selected_commits=[{"row_id": "1"}],
            target_path="/t",
            patch_dataset_dir="pd",
            signer_name="n",
            signer_email="e",
            commit_message_template="tpl",
            commit_message_source="openEuler",
            linux_repo_path="lr",
            target_config_layout="none",
            target_config_layout_opts=None,
        )
        assert "target_config_layout" not in captured_config
        assert "target_config_layout_opts" not in captured_config


    def test_continue_report_writes_layout_to_config(
        self,
        client: BackportCvekitClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """continue_report 写入当前 layout 到 CVEKit YAML"""
        base = _archive_task(client, tmp_path) / "b.yml"
        _write_report(
            base,
            commits=[
                {"row_id": "1", "status": "success"},
                {"row_id": "2", "status": "pending"},
            ],
            target_config_layout="none",
        )

        captured_config: dict[str, Any] = {}

        def fake_run(self, args, cwd, **kwargs):
            for a in args:
                if a.endswith(".report.yml"):
                    captured_config.update(
                        yaml.safe_load(Path(a).read_text(encoding="utf-8")) or {}
                    )
            cfg = next(a for a in args if a.endswith(".report.yml"))
            Path(cfg).write_text(
                yaml.safe_dump(
                    {
                        "commits": [
                            {"row_id": "1", "status": "success"},
                            {"row_id": "2", "status": "success"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
        client.continue_report(
            base_report_path=str(base),
            target_config_layout="anolis",
            target_config_layout_opts={"default_level": "L0-MANDATORY"},
        )
        assert captured_config.get("target_config_layout") == "anolis"
        assert captured_config.get("target_config_layout_opts") == {
            "default_level": "L0-MANDATORY"
        }

    def test_preview_commit_message_sanitizes_layout_and_uses_report_suffix(
        self,
        client: BackportCvekitClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """preview 传 .report.yml 配置，layout=none 清掉旧 layout"""
        base = tmp_path / "b.yml"
        _write_report(
            base,
            commits=[{"row_id": "1", "status": "pending", "patch_path": "/p"}],
            target_config_layout="anolis",
            target_config_layout_opts={"default_level": "L2-OPTIONAL"},
        )

        captured_config_path: str = ""
        captured_config: dict[str, Any] = {}

        def fake_run(self, args, cwd, **kwargs):
            nonlocal captured_config_path
            for i, a in enumerate(args):
                if a == "--backport-config" and i + 1 < len(args):
                    captured_config_path = args[i + 1]
            if captured_config_path:
                captured_config.update(
                    yaml.safe_load(
                        Path(captured_config_path).read_text(encoding="utf-8")
                    )
                    or {}
                )
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps({"status": "success", "commit_message": "fix: test"}),
                stderr="",
            )

        monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
        client.preview_commit_message(
            base_report_path=str(base),
            row={"row_id": "1"},
            commit_message_template="tpl",
            commit_message_source="auto",
            linux_repo_path="lr",
            target_config_layout="none",
            target_config_layout_opts=None,
        )
        # 验证传给 CVEKit 的配置文件以 .report.yml 结尾
        assert captured_config_path.endswith(".report.yml"), (
            f"preview config should end with .report.yml, got: {captured_config_path}"
        )
        # layout=none 时旧 layout 被清掉
        assert "target_config_layout" not in captured_config
        assert "target_config_layout_opts" not in captured_config


# ── 前置提交查找 ─────────────────────────────────────────────


def test_merge_prerequisite_commits_deduplicates_and_preserves_metadata(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "backport-batch.yml"
    _write_report(config_path, commits=[{"commit": "aaaa", "commit_title": "original"}])

    added = BackportCvekitClient._merge_prerequisite_commits(
        config_path,
        [
            {"commit": "aaaa", "title": "duplicate"},
            {
                "commit": "bbbb",
                "title": "provider",
                "required_by": ["aaaa"],
                "capabilities": ["symbol:helper"],
            },
        ],
    )

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert added == 1
    assert [row["commit"] for row in data["commits"]] == ["aaaa", "bbbb"]
    assert data["commits"][1]["origin"] == "prerequisite"
    assert data["commits"][1]["required_by"] == ["aaaa"]


def test_prerequisite_commits_uses_read_only_cli_without_runtime_model(
    tmp_path: Path,
    fake_cvekit: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    client = BackportCvekitClient(runs_root=tmp_path / "runs")
    captured: dict[str, Any] = {}

    def fake_run(self, args, cwd, *, require_runtime_config=True):
        captured.update(
            args=args,
            cwd=cwd,
            require_runtime_config=require_runtime_config,
        )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"status": "ready", "candidates": []}),
            stderr="",
        )

    monkeypatch.setattr(BackportCvekitClient, "_run_cvekit", fake_run)
    result = client.prerequisite_commits(
        source_repo=str(source),
        target_repo="/tmp/target",
        target_ref="target-sha",
        prereq_commits=["aaaa", "bbbb"],
    )

    assert result["status"] == "ready"
    assert captured["require_runtime_config"] is False
    assert captured["cwd"] == source
    assert captured["args"].count("--prereq-commit") == 2

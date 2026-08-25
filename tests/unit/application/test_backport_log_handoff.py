import re
import subprocess
from pathlib import Path

import pytest

from witty_service.application.backport_cvekit_client import (
    BackportCvekitClient,
    BackportRuntimeConfig,
)
from witty_service.application.backport_run_store import BackportRunStore


def test_run_cvekit_copies_cli_and_engine_logs_into_attempt_preserving_sources(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    batch_log = tmp_path / ".patchflow" / "logs" / "batch" / "patchflow-batch-test.log"
    engine_log = (
        tmp_path / ".patchflow" / "logs" / "opencode" / "patchflow-opencode-test.log"
    )
    batch_log.parent.mkdir(parents=True)
    engine_log.parent.mkdir(parents=True)
    batch_log.write_text("batch api_key=secret-value\n", encoding="utf-8")
    engine_log.write_text("engine\n", encoding="utf-8")
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    client = BackportCvekitClient(runs_root=tmp_path / "runs")
    client.set_runtime_config(
        BackportRuntimeConfig(
            llm_provider="openai",
            api_key="secret-value",
        )
    )
    monkeypatch.setattr(client, "resolve_cvekit_path", lambda: tmp_path / "cvekit")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="{}",
            stderr=(f"patchflow-batch 日志文件: {batch_log}\n日志文件: {engine_log}\n"),
        ),
    )

    client._run_cvekit(["--action", "backport-batch"], attempt)

    # 源日志保留在 ~/.patchflow/logs,内容未被 Witty 改写(源日志不脱敏)
    assert batch_log.read_text(encoding="utf-8") == "batch api_key=secret-value\n"
    assert engine_log.read_text(encoding="utf-8") == "engine\n"
    # 归档副本仍脱敏
    assert "secret-value" not in (attempt / batch_log.name).read_text(encoding="utf-8")
    # 新命名下引擎日志归档带 {engine}- 前缀(opencode/ 目录),保留引擎信息
    assert (attempt / f"opencode-{engine_log.name}").read_text(
        encoding="utf-8"
    ) == "engine\n"


def test_run_cvekit_assigns_distinct_run_ids_same_task_id(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    client = BackportCvekitClient(runs_root=tmp_path / "runs")
    client.set_runtime_config(
        BackportRuntimeConfig(
            llm_provider="openai",
            api_key="secret-value",
        )
    )
    task_id = "20260812-excel-kernel-a1b2c3d4"
    client.set_archive_run_id(task_id)
    monkeypatch.setattr(client, "resolve_cvekit_path", lambda: tmp_path / "cvekit")
    captured_envs: list[dict[str, str]] = []

    def fake_run(args, **kwargs):
        captured_envs.append(kwargs["env"])
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="{}",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    client._run_cvekit(["--action", "backport-batch"], tmp_path / "workdir")
    client._run_cvekit(["--action", "apply-row"], tmp_path / "workdir")

    # 同 task 连续多次调用:每次独立 cvekit_run_id(供 patchflow 文件名 run_id8),
    # CVEKIT_TASK_ID 稳定不变
    assert len(captured_envs) == 2
    run_ids = [env["CVEKIT_RUN_ID"] for env in captured_envs]
    assert run_ids[0] != run_ids[1]
    for run_id in run_ids:
        assert re.fullmatch(r"\d{14}-[0-9a-f]{8}", run_id)
    assert {env["CVEKIT_TASK_ID"] for env in captured_envs} == {task_id}


def test_run_cvekit_launch_failure_completes_run_as_failed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    client = BackportCvekitClient(runs_root=tmp_path / "runs")
    client.set_runtime_config(
        BackportRuntimeConfig(
            llm_provider="openai",
            api_key="secret-value",
        )
    )
    task_id = "20260812-excel-kernel-a1b2c3d4"
    client.set_archive_run_id(task_id)

    def boom(*args, **kwargs):
        raise FileNotFoundError("cvekit binary missing")

    monkeypatch.setattr(client, "resolve_cvekit_path", boom)

    with pytest.raises(FileNotFoundError):
        client._run_cvekit(["--action", "backport-batch"], tmp_path / "workdir")

    # 子进程启动异常也关闭登记记录,不遗留 running/无 ended_at
    runs = client._run_store.read_manifest(client._run_store._task_dir(task_id))[
        "logging"
    ]["cvekit_runs"]
    assert runs[0]["status"] == "failed"
    assert runs[0]["ended_at"]


def test_run_cvekit_records_explicit_operation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    client = BackportCvekitClient(runs_root=tmp_path / "runs")
    client.set_runtime_config(
        BackportRuntimeConfig(
            llm_provider="openai",
            api_key="secret-value",
        )
    )
    task_id = "20260812-excel-kernel-a1b2c3d4"
    client.set_archive_run_id(task_id)
    monkeypatch.setattr(client, "resolve_cvekit_path", lambda: tmp_path / "cvekit")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="{}",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    client._run_cvekit(
        ["--action", "backport-batch"],
        tmp_path / "workdir",
        operation="apply_row",
    )

    runs = client._run_store.read_manifest(client._run_store._task_dir(task_id))[
        "logging"
    ]["cvekit_runs"]
    assert runs[0]["operation"] == "apply_row"


def test_case_archive_resolves_logs_already_moved_into_attempt(tmp_path: Path) -> None:
    excel = tmp_path / "input.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")
    task_dir = store.create_task(
        excel_path=excel,
        target_repository="/repo/kernel",
    )
    store.update_manifest(task_dir, {"status": "running", "current_run": 1})
    attempt = store.next_attempt_dir(task_dir / "cases")
    batch_name = "patchflow-apply-test.log"
    engine_name = "opencode-patchflow-20260810-120000-a1b2c3d4e5f6.log"
    (attempt / batch_name).write_text("batch\n", encoding="utf-8")
    (attempt / engine_name).write_text("engine\n", encoding="utf-8")
    row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "a" * 40,
        "status": "success",
        "log_path": str(
            tmp_path
            / ".patchflow"
            / "logs"
            / "opencode"
            / engine_name.removeprefix("opencode-")
        ),
        "batch_log_path": str(tmp_path / ".patchflow" / "logs" / "batch" / batch_name),
        "backport_engine": "opencode",
    }

    result = store.archive_case_attempt(
        attempt_dir=attempt,
        report_path=attempt / "report.yml",
        rows=[row],
        sanitized_rows=[row],
        patch_keys=(),
        result={"operation": "apply_row", "status": "success"},
    )

    case_dir = task_dir / "cases" / result["case_id"]
    artifacts = result["artifacts"]
    assert (case_dir / artifacts["apply_log"]).read_text(encoding="utf-8") == "batch\n"
    assert (case_dir / artifacts["engine_log"]).read_text(
        encoding="utf-8"
    ) == "engine\n"


def test_case_archive_falls_back_to_legacy_fields(tmp_path: Path) -> None:
    """历史 report(batch_logfile/logfile 旧字段)仍能归档(过渡期兼容)。"""
    excel = tmp_path / "input.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")
    task_dir = store.create_task(
        excel_path=excel,
        target_repository="/repo/kernel",
    )
    store.update_manifest(task_dir, {"status": "running", "current_run": 1})
    attempt = store.next_attempt_dir(task_dir / "cases")
    batch_name = "patchflow-batch-test.log"
    (attempt / batch_name).write_text("batch\n", encoding="utf-8")
    row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "a" * 40,
        "status": "success",
        # 旧字段:batch_logfile(新字段 log_path 缺失)
        "batch_logfile": str(tmp_path / ".patchflow" / "logs" / "batch" / batch_name),
    }

    result = store.archive_case_attempt(
        attempt_dir=attempt,
        report_path=attempt / "report.yml",
        rows=[row],
        sanitized_rows=[row],
        patch_keys=(),
        result={"operation": "apply_row", "status": "success"},
    )

    case_dir = task_dir / "cases" / result["case_id"]
    artifacts = result["artifacts"]
    assert "apply_log" in artifacts
    assert (case_dir / artifacts["apply_log"]).read_text(encoding="utf-8") == "batch\n"


def test_case_archive_legacy_log_path_is_only_a_batch_log(tmp_path: Path) -> None:
    """旧 report 仅有 log_path 时，不能把 batch 日志重复标记为 engine_log。"""
    excel = tmp_path / "input.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")
    task_dir = store.create_task(excel_path=excel, target_repository="/repo/kernel")
    store.update_manifest(task_dir, {"status": "running", "current_run": 1})
    attempt = store.next_attempt_dir(task_dir / "cases")
    batch_name = "patchflow-batch-test.log"
    (attempt / batch_name).write_text("batch\n", encoding="utf-8")
    row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "a" * 40,
        "status": "success",
        "log_path": str(tmp_path / ".patchflow" / "logs" / "batch" / batch_name),
        "backport_engine": "mystique",
    }

    result = store.archive_case_attempt(
        attempt_dir=attempt,
        report_path=attempt / "report.yml",
        rows=[row],
        sanitized_rows=[row],
        patch_keys=(),
        result={"operation": "execute_selected", "status": "success"},
    )

    case_dir = task_dir / "cases" / result["case_id"]
    artifacts = result["artifacts"]
    assert (case_dir / artifacts["backport_log"]).read_text(encoding="utf-8") == "batch\n"
    assert "engine_log" not in artifacts


def test_case_archive_skips_backfilled_batch_log_as_engine_log(tmp_path: Path) -> None:
    """未建引擎分段时，Patchflow 回填的 batch log_path 不能伪造 engine_log。"""
    excel = tmp_path / "input.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")
    task_dir = store.create_task(excel_path=excel, target_repository="/repo/kernel")
    store.update_manifest(task_dir, {"status": "running", "current_run": 1})
    attempt = store.next_attempt_dir(task_dir / "cases")
    batch_name = "patchflow-batch-test.log"
    batch_path = tmp_path / ".patchflow" / "logs" / "batch" / batch_name
    (attempt / batch_name).write_text("batch\n", encoding="utf-8")
    row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "a" * 40,
        "status": "failed",
        "batch_log_path": str(batch_path),
        "log_path": str(batch_path),
        "backport_engine": "mystique",
    }

    result = store.archive_case_attempt(
        attempt_dir=attempt,
        report_path=attempt / "report.yml",
        rows=[row],
        sanitized_rows=[row],
        patch_keys=(),
        result={"operation": "execute_selected", "status": "failed"},
    )

    case_dir = task_dir / "cases" / result["case_id"]
    artifacts = result["artifacts"]
    assert (case_dir / artifacts["backport_log"]).read_text(encoding="utf-8") == "batch\n"
    assert "engine_log" not in artifacts


def test_case_archive_legacy_logfile_is_an_engine_log(tmp_path: Path) -> None:
    """旧 report 仅有 logfile 时，仍归档引擎日志而不伪造 batch 日志。"""
    excel = tmp_path / "input.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")
    task_dir = store.create_task(excel_path=excel, target_repository="/repo/kernel")
    store.update_manifest(task_dir, {"status": "running", "current_run": 1})
    attempt = store.next_attempt_dir(task_dir / "cases")
    engine_name = "opencode-patchflow-20260810-120000-a1b2c3d4e5f6.log"
    (attempt / engine_name).write_text("engine\n", encoding="utf-8")
    row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "a" * 40,
        "status": "success",
        "logfile": str(
            tmp_path
            / ".patchflow"
            / "logs"
            / "opencode"
            / engine_name.removeprefix("opencode-")
        ),
        "backport_engine": "opencode",
    }

    result = store.archive_case_attempt(
        attempt_dir=attempt,
        report_path=attempt / "report.yml",
        rows=[row],
        sanitized_rows=[row],
        patch_keys=(),
        result={"operation": "execute_selected", "status": "success"},
    )

    case_dir = task_dir / "cases" / result["case_id"]
    artifacts = result["artifacts"]
    assert (case_dir / artifacts["engine_log"]).read_text(encoding="utf-8") == "engine\n"
    assert "backport_log" not in artifacts


def test_case_archives_only_its_engine_log_from_shared_attempt(tmp_path: Path) -> None:
    """同一 attempt 的多个 case 只归档归属自己的引擎日志。"""
    excel = tmp_path / "input.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")
    task_dir = store.create_task(
        excel_path=excel,
        target_repository="/repo/kernel",
    )
    store.update_manifest(task_dir, {"status": "running", "current_run": 1})
    attempt = store.next_attempt_dir(task_dir / "cases")
    batch_name = "patchflow-batch-test.log"
    eng1 = "portgpt-patchflow-20260810-120000-a1b2c3d4e5f6.log"
    eng2 = "opencode-patchflow-20260810-120000-f6e5d4c3b2a1.log"
    (attempt / batch_name).write_text("batch\n", encoding="utf-8")
    (attempt / eng1).write_text("engine1\n", encoding="utf-8")
    (attempt / eng2).write_text("engine2\n", encoding="utf-8")
    portgpt_row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "a" * 40,
        "status": "success",
        "log_path": str(
            tmp_path / ".patchflow" / "logs" / "portgpt" / eng1.removeprefix("portgpt-")
        ),
        "batch_log_path": str(tmp_path / ".patchflow" / "logs" / "batch" / batch_name),
        "backport_engine": "portgpt",
    }
    opencode_row = {
        **portgpt_row,
        "row_id": "2",
        "row_number": 2,
        "commit": "b" * 40,
        "log_path": str(
            tmp_path / ".patchflow" / "logs" / "opencode" / eng2.removeprefix("opencode-")
        ),
        "backport_engine": "opencode",
    }

    portgpt_result = store.archive_case_attempt(
        attempt_dir=attempt,
        report_path=attempt / "report.yml",
        rows=[portgpt_row],
        sanitized_rows=[portgpt_row],
        patch_keys=(),
        result={"operation": "execute_selected", "status": "success"},
        cleanup=False,
    )
    opencode_result = store.archive_case_attempt(
        attempt_dir=attempt,
        report_path=attempt / "report.yml",
        rows=[opencode_row],
        sanitized_rows=[opencode_row],
        patch_keys=(),
        result={"operation": "execute_selected", "status": "success"},
        cleanup=False,
    )

    portgpt_artifacts = portgpt_result["artifacts"]
    opencode_artifacts = opencode_result["artifacts"]
    portgpt_case = task_dir / "cases" / portgpt_result["case_id"]
    opencode_case = task_dir / "cases" / opencode_result["case_id"]
    assert (portgpt_case / portgpt_artifacts["engine_log"]).read_text(encoding="utf-8") == "engine1\n"
    assert (opencode_case / opencode_artifacts["engine_log"]).read_text(encoding="utf-8") == "engine2\n"
    assert "engine_log_2" not in portgpt_artifacts
    assert "engine_log_2" not in opencode_artifacts


def test_case_archive_engine_logs_production_naming(tmp_path: Path) -> None:
    """同基底名的多引擎日志按当前 row 的引擎前缀精确选择。"""
    excel = tmp_path / "input.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")
    task_dir = store.create_task(
        excel_path=excel,
        target_repository="/repo/kernel",
    )
    store.update_manifest(task_dir, {"status": "running", "current_run": 1})
    attempt = store.next_attempt_dir(task_dir / "cases")
    batch_name = "patchflow-20260810-120000-a1b2c3d4e5f6.log"
    eng1 = "portgpt-patchflow-20260810-120000-a1b2c3d4e5f6.log"
    eng2 = "opencode-patchflow-20260810-120000-a1b2c3d4e5f6.log"
    (attempt / batch_name).write_text("batch\n", encoding="utf-8")
    (attempt / eng1).write_text("engine1\n", encoding="utf-8")
    (attempt / eng2).write_text("engine2\n", encoding="utf-8")
    row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "a" * 40,
        "status": "success",
        "log_path": str(
            tmp_path / ".patchflow" / "logs" / "opencode" / eng2.removeprefix("opencode-")
        ),
        "batch_log_path": str(tmp_path / ".patchflow" / "logs" / "batch" / batch_name),
        "backport_engine": "opencode",
    }

    result = store.archive_case_attempt(
        attempt_dir=attempt,
        report_path=attempt / "report.yml",
        rows=[row],
        sanitized_rows=[row],
        patch_keys=(),
        result={"operation": "execute_selected", "status": "success"},
    )

    case_dir = task_dir / "cases" / result["case_id"]
    artifacts = result["artifacts"]
    assert (case_dir / artifacts["engine_log"]).read_text(encoding="utf-8") == "engine2\n"
    assert "engine_log_2" not in artifacts


def test_case_archive_separates_batch_and_mystique_log_paths(
    monkeypatch, tmp_path: Path
) -> None:
    """batch 主日志与 Mystique 分段日志必须分别归档且不重复。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    excel = tmp_path / "input.xlsx"
    excel.write_text("fixture", encoding="utf-8")
    store = BackportRunStore(tmp_path / "runs")
    task_dir = store.create_task(
        excel_path=excel,
        target_repository="/repo/kernel",
    )
    store.update_manifest(task_dir, {"status": "running", "current_run": 1})
    attempt = store.next_attempt_dir(task_dir / "cases")
    batch_name = "patchflow-20260810-120000-a1b2c3d4-all.log"
    mystique_name = "mystique-patchflow-20260810-120000-a1b2c3d4-deadbeef.log"
    mystique_log_path = (
        tmp_path
        / ".patchflow"
        / "logs"
        / "mystique"
        / mystique_name.removeprefix("mystique-")
    )
    (attempt / batch_name).write_text("batch\n", encoding="utf-8")
    (attempt / mystique_name).write_text("mystique\n", encoding="utf-8")
    row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "a" * 40,
        "status": "success",
        "log_path": str(mystique_log_path),
        "batch_log_path": str(tmp_path / ".patchflow" / "logs" / "batch" / batch_name),
        "backport_engine": "mystique",
    }

    result = store.archive_case_attempt(
        attempt_dir=attempt,
        report_path=attempt / "report.yml",
        rows=[row],
        sanitized_rows=[row],
        patch_keys=(),
        result={"operation": "execute_selected", "status": "success"},
    )

    case_dir = task_dir / "cases" / result["case_id"]
    artifacts = result["artifacts"]
    assert (case_dir / artifacts["backport_log"]).read_text(encoding="utf-8") == "batch\n"
    assert (case_dir / artifacts["engine_log"]).read_text(encoding="utf-8") == "mystique\n"
    assert "engine_log_2" not in artifacts

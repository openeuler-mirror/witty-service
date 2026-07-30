from pathlib import Path
import subprocess

from witty_service.application.backport_cvekit_client import (
    BackportCvekitClient,
    BackportRuntimeConfig,
)
from witty_service.application.backport_run_store import BackportRunStore


def test_run_cvekit_moves_cli_and_engine_logs_into_attempt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    batch_log = tmp_path / ".backport" / "backport-batch-test.log"
    engine_log = (
        tmp_path
        / ".cvekit"
        / "backport_logs"
        / "backport-opencode-test.log"
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
            stderr=(
                f"backport-batch 日志文件: {batch_log}\n"
                f"日志文件: {engine_log}\n"
            ),
        ),
    )

    client._run_cvekit(["--action", "backport-batch"], attempt)

    assert not batch_log.exists()
    assert not engine_log.exists()
    assert "secret-value" not in (attempt / batch_log.name).read_text(
        encoding="utf-8"
    )
    assert (attempt / engine_log.name).read_text(encoding="utf-8") == "engine\n"


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
    batch_name = "backport-apply-test.log"
    engine_name = "backport-opencode-test.log"
    (attempt / batch_name).write_text("batch\n", encoding="utf-8")
    (attempt / engine_name).write_text("engine\n", encoding="utf-8")
    row = {
        "row_id": "1",
        "row_number": 1,
        "commit": "a" * 40,
        "status": "success",
        "batch_logfile": str(tmp_path / ".backport" / batch_name),
        "logfile": str(tmp_path / ".cvekit" / "backport_logs" / engine_name),
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
    assert (case_dir / artifacts["engine_log"]).read_text(encoding="utf-8") == "engine\n"

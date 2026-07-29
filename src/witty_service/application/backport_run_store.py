from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


class BackportRunStore:
    """Persists Backport run state and immutable attempt artifacts."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, runs_root: str | Path) -> None:
        self.runs_root = Path(runs_root).expanduser().resolve()
        self._run_id = ""
        self._command_sequence = 0

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def safe_slug(value: str, *, fallback: str = "item", max_length: int = 96) -> str:
        text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
        if not text:
            text = fallback
        return text[:max_length]

    def set_run_id(self, run_id: str | None) -> None:
        self._run_id = self.safe_slug(run_id or "", fallback="")

    def _lock_for(self, run_dir: Path) -> threading.RLock:
        key = str(run_dir.resolve())
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    def _run_root_for(self, path: Path) -> Path | None:
        resolved = path.expanduser().resolve()
        for candidate in (resolved, *resolved.parents):
            if candidate.parent == self.runs_root:
                return candidate
        return None

    @staticmethod
    def _execution_dir_for(path: Path) -> Path | None:
        resolved = path.expanduser().resolve()
        for candidate in (resolved, *resolved.parents):
            if candidate.parent.name == "executions":
                return candidate
        return None

    def _ensure_run_dirs(self, run_dir: Path) -> None:
        for name in ("input", "reports", "cases", "logs", "executions"):
            (run_dir / name).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _ensure_execution_dirs(execution_dir: Path) -> None:
        for name in ("input", "reports", "cases", "logs"):
            (execution_dir / name).mkdir(parents=True, exist_ok=True)

    def write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def read_manifest(self, run_dir_or_id: Path | str) -> dict[str, Any]:
        candidate = Path(run_dir_or_id)
        run_dir = (
            (self.runs_root / self.safe_slug(str(run_dir_or_id))).resolve()
            if not candidate.is_absolute()
            else self._run_root_for(candidate) or candidate.resolve()
        )
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            return {"schema_version": 2, "run_id": run_dir.name}
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"schema_version": 2, "run_id": run_dir.name, "status": "corrupted"}
        return loaded if isinstance(loaded, dict) else {
            "schema_version": 2,
            "run_id": run_dir.name,
            "status": "corrupted",
        }

    def update_manifest(self, run_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
        run_root = self._run_root_for(run_dir) or run_dir.resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        self._ensure_run_dirs(run_root)
        with self._lock_for(run_root):
            manifest = self.read_manifest(run_root)
            manifest.setdefault("schema_version", 2)
            manifest.setdefault("run_id", run_root.name)
            manifest.update(updates)
            manifest["updated_at"] = self.now_iso()
            self.write_json(run_root / "manifest.json", manifest)
            return manifest

    def update_current_execution(self, run_dir: Path, updates: dict[str, Any]) -> None:
        run_root = self._run_root_for(run_dir) or run_dir.resolve()
        with self._lock_for(run_root):
            manifest = self.read_manifest(run_root)
            raw_execution_dir = str(manifest.get("current_execution_dir") or "").strip()
            if not raw_execution_dir:
                return
            execution_dir = Path(raw_execution_dir).expanduser().resolve()
            if self._run_root_for(execution_dir) != run_root:
                return
            execution_path = execution_dir / "execution.json"
            if execution_path.is_file():
                try:
                    loaded = json.loads(execution_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    loaded = {}
            else:
                loaded = {}
            execution = loaded if isinstance(loaded, dict) else {}
            execution.update(updates)
            execution["updated_at"] = self.now_iso()
            self.write_json(execution_path, execution)

    def append_run_log(self, run_dir: Path, message: str) -> None:
        run_root = self._run_root_for(run_dir) or run_dir.resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        with self._lock_for(run_root):
            with (run_root / "run.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{self.now_iso()} {message}\n")

    def create_async_record(
        self,
        *,
        run_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        safe_run_id = self.safe_slug(run_id)
        run_dir = (self.runs_root / safe_run_id).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_run_dirs(run_dir)
        existing = self.read_manifest(run_dir)
        excel_path = str(payload.get("excel_path") or payload.get("excelPath") or "").strip()
        created_at = str(existing.get("created_at") or self.now_iso())
        record = self.update_manifest(
            run_dir,
            {
                "run_id": safe_run_id,
                "action": action,
                "status": "running",
                "result": None,
                "error": "",
                "progress": None,
                "pause_requested": False,
                "paused_at": None,
                "created_at": created_at,
                "display_name": str(existing.get("display_name") or Path(excel_path).name or safe_run_id),
            },
        )
        self.write_json(run_dir / "input" / "request.json", payload)
        if action != "generate_report":
            self.update_current_execution(
                run_dir,
                {"action": action, "status": "running"},
            )
        self.append_run_log(run_dir, f"async action started action={action}")
        return self.as_async_record(record)

    @staticmethod
    def as_async_record(manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": str(manifest.get("run_id") or ""),
            "action": str(manifest.get("action") or "run_all"),
            "status": str(manifest.get("status") or "interrupted"),
            "result": manifest.get("result") if isinstance(manifest.get("result"), dict) else None,
            "error": str(manifest.get("error") or ""),
            "progress": manifest.get("progress") if isinstance(manifest.get("progress"), dict) else None,
            "pause_requested": bool(manifest.get("pause_requested")),
            "paused_at": manifest.get("paused_at"),
        }

    def get_async_record(self, run_id: str, *, active: bool) -> dict[str, Any] | None:
        run_dir = (self.runs_root / self.safe_slug(run_id)).resolve()
        if not (run_dir / "manifest.json").exists():
            return None
        manifest = self.read_manifest(run_dir)
        if manifest.get("status") == "running" and not active:
            manifest = self.update_manifest(
                run_dir,
                {
                    "status": "interrupted",
                    "error": str(manifest.get("error") or "witty-service restarted while the run was active"),
                },
            )
            self.append_run_log(run_dir, "run marked interrupted because no active worker exists")
        return self.as_async_record(manifest)

    def list_runs(self, *, active_run_ids: set[str] | None = None) -> list[dict[str, Any]]:
        active_ids = active_run_ids or set()
        if not self.runs_root.exists():
            return []
        items: list[dict[str, Any]] = []
        for run_dir in self.runs_root.iterdir():
            if not run_dir.is_dir() or not (run_dir / "manifest.json").exists():
                continue
            record = self.get_async_record(run_dir.name, active=run_dir.name in active_ids)
            if record is None:
                continue
            manifest = self.read_manifest(run_dir)
            record.update(
                {
                    "display_name": str(manifest.get("display_name") or run_dir.name),
                    "created_at": str(manifest.get("created_at") or ""),
                    "updated_at": str(manifest.get("updated_at") or ""),
                    "current_report_path": str(
                        manifest.get("latest_report_path")
                        or manifest.get("report_path")
                        or manifest.get("current_report_path")
                        or ""
                    ),
                    "excel_path": str(manifest.get("excel_path") or ""),
                    "commit_count": int(manifest.get("commit_count") or 0),
                    "current_excel_version": int(manifest.get("current_excel_version") or 0),
                    "current_execution": int(manifest.get("current_execution") or 0),
                    "run_dir": str(run_dir),
                }
            )
            items.append(record)
        return sorted(items, key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    def list_executions(self, run_id: str) -> list[dict[str, Any]]:
        run_dir = (self.runs_root / self.safe_slug(run_id)).resolve()
        executions_dir = run_dir / "executions"
        items: list[dict[str, Any]] = []
        if executions_dir.is_dir():
            for execution_dir in executions_dir.iterdir():
                if not execution_dir.is_dir() or not execution_dir.name.isdigit():
                    continue
                execution_path = execution_dir / "execution.json"
                if execution_path.is_file():
                    try:
                        loaded = json.loads(execution_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        loaded = {}
                else:
                    loaded = {}
                execution = loaded if isinstance(loaded, dict) else {}
                report_candidates = [
                    execution_dir / "reports" / "latest.report.yml",
                    execution_dir / "reports" / "backport-batch.yml.report.yml",
                ]
                result = execution.get("result")
                parsed_result = result.get("parsedResult") if isinstance(result, dict) else None
                artifacts = (
                    parsed_result.get("artifacts")
                    if isinstance(parsed_result, dict)
                    and isinstance(parsed_result.get("artifacts"), dict)
                    else {}
                )
                result_report_path = str(
                    artifacts.get("base_report_path")
                    or artifacts.get("report_path")
                    or ""
                )
                report_path = result_report_path or next(
                    (str(path) for path in report_candidates if path.is_file()),
                    "",
                )
                items.append(
                    {
                        "execution": int(execution_dir.name),
                        "status": str(execution.get("status") or "interrupted"),
                        "action": str(execution.get("action") or execution.get("operation") or ""),
                        "created_at": str(execution.get("created_at") or ""),
                        "updated_at": str(execution.get("updated_at") or ""),
                        "report_path": report_path,
                        "target": execution.get("target")
                        if isinstance(execution.get("target"), dict)
                        else {},
                        "excel_version": int(execution.get("excel_version") or 0),
                    }
                )
        return sorted(items, key=lambda item: int(item["execution"]), reverse=True)

    def create_run_dir(
        self,
        *,
        operation: str,
        request: dict[str, Any] | None = None,
    ) -> Path:
        self.runs_root.mkdir(parents=True, exist_ok=True)
        if self._run_id:
            run_id = self._run_id
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_id = f"backport_{stamp}_{uuid.uuid4().hex[:8]}"
        run_root = (self.runs_root / run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        self._ensure_run_dirs(run_root)
        existing = self.read_manifest(run_root)
        current_execution = int(existing.get("current_execution") or 0)
        execution_number = current_execution + 1
        execution_dir = run_root / "executions" / f"{execution_number:03d}"
        execution_dir.mkdir(parents=True, exist_ok=False)
        self._ensure_execution_dirs(execution_dir)
        if request is not None:
            self.write_json(execution_dir / "input" / "request.json", request)
            self.write_json(run_root / "input" / "request.json", request)
        self.update_manifest(
            run_root,
            {
                "run_id": run_id,
                "operation": operation,
                "status": "running",
                "created_at": str(existing.get("created_at") or self.now_iso()),
                "current_execution": execution_number,
                "current_execution_dir": str(execution_dir),
                "paths": {
                    "input_dir": str(execution_dir / "input"),
                    "reports_dir": str(execution_dir / "reports"),
                    "cases_dir": str(execution_dir / "cases"),
                    "logs_dir": str(execution_dir / "logs"),
                },
            },
        )
        self.write_json(
            execution_dir / "execution.json",
            {
                "execution": execution_number,
                "operation": operation,
                "status": "running",
                "created_at": self.now_iso(),
                "request": request or {},
            },
        )
        self.append_run_log(
            run_root,
            f"execution {execution_number:03d} ready operation={operation}",
        )
        return execution_dir

    def archive_excel(self, run_dir: Path, excel_path: Path) -> int:
        run_root = self._run_root_for(run_dir) or run_dir.resolve()
        content_hash = hashlib.sha256(excel_path.read_bytes()).hexdigest()
        versions_dir = run_root / "input" / "excel"
        versions_dir.mkdir(parents=True, exist_ok=True)
        for version_dir in versions_dir.iterdir():
            metadata_path = version_dir / "metadata.json"
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if metadata.get("sha256") == content_hash:
                version = int(version_dir.name)
                self.update_manifest(
                    run_root,
                    {
                        "display_name": excel_path.name,
                        "current_excel_version": version,
                        "excel_path": str(version_dir / f"source{excel_path.suffix.lower()}"),
                    },
                )
                return version
        existing = [int(item.name) for item in versions_dir.iterdir() if item.is_dir() and item.name.isdigit()]
        version = max(existing, default=0) + 1
        version_dir = versions_dir / f"{version:03d}"
        version_dir.mkdir(parents=True, exist_ok=False)
        archived_path = version_dir / f"source{excel_path.suffix.lower()}"
        shutil.copy2(excel_path, archived_path)
        self.write_json(
            version_dir / "metadata.json",
            {
                "version": version,
                "original_name": excel_path.name,
                "original_path": str(excel_path),
                "archive_path": str(archived_path),
                "sha256": content_hash,
                "imported_at": self.now_iso(),
            },
        )
        self.update_manifest(
            run_root,
            {
                "display_name": excel_path.name,
                "current_excel_version": version,
                "excel_path": str(archived_path),
            },
        )
        return version

    def ensure_for_report(self, report_path: Path) -> Path:
        path = report_path.expanduser().resolve()
        existing_root = self._run_root_for(path)
        if existing_root is not None:
            self._ensure_run_dirs(existing_root)
            if not (existing_root / "manifest.json").exists():
                self.update_manifest(
                    existing_root,
                    {
                        "run_id": existing_root.name,
                        "operation": "continued",
                        "action": "run_all",
                        "status": "interrupted",
                        "created_at": self.now_iso(),
                        "source_report_path": str(path),
                        "report_path": str(path),
                        "display_name": existing_root.name,
                    },
                )
            execution_dir = self._execution_dir_for(path)
            if execution_dir is not None:
                self._ensure_execution_dirs(execution_dir)
                return execution_dir
            return existing_root
        if self._run_id:
            run_dir = self.runs_root / self._run_id
        else:
            run_dir = self.runs_root / f"continued_{self.safe_slug(path.stem, fallback='report')}_{uuid.uuid4().hex[:8]}"
        run_dir = run_dir.resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_run_dirs(run_dir)
        if not (run_dir / "manifest.json").exists():
            self.update_manifest(
                run_dir,
                {
                    "run_id": run_dir.name,
                    "operation": "continued",
                    "action": "run_all",
                    "status": "running",
                    "created_at": self.now_iso(),
                    "source_report_path": str(path),
                    "report_path": str(path),
                },
            )
        return run_dir

    def write_command_logs(
        self,
        *,
        cwd: Path,
        command: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self._command_sequence += 1
        run_root = self._run_root_for(cwd)
        if cwd.name.isdigit() and cwd.parent.name == "attempts":
            log_dir = cwd
            prefix = ""
        elif run_root is not None:
            execution_dir = self._execution_dir_for(cwd)
            log_dir = (execution_dir or run_root) / "logs"
            prefix = f"cvekit-{self._command_sequence:03d}."
        else:
            log_dir = cwd / "logs"
            prefix = f"cvekit-{self._command_sequence:03d}."
        log_dir.mkdir(parents=True, exist_ok=True)
        self.write_json(
            log_dir / f"{prefix}command.json",
            {
                "command": command,
                "cwd": str(cwd),
                "started_at": self.now_iso(),
                "returncode": returncode,
            },
        )
        (log_dir / f"{prefix}cvekit.stdout.log").write_text(stdout, encoding="utf-8")
        (log_dir / f"{prefix}cvekit.stderr.log").write_text(stderr, encoding="utf-8")
        if run_root is not None:
            self.append_run_log(run_root, f"cvekit command returncode={returncode} cwd={cwd}")

    def case_dir(self, run_dir: Path, row: dict[str, Any], *, row_id: str) -> Path:
        commit = str(row.get("commit") or row.get("input_commit") or row_id)
        case_name = (
            f"{self.safe_slug(row_id, fallback='row', max_length=72)}_"
            f"{self.safe_slug(commit[:12], fallback='commit')}"
        )
        case_dir = run_dir / "cases" / case_name
        (case_dir / "attempts").mkdir(parents=True, exist_ok=True)
        return case_dir

    @staticmethod
    def next_attempt_dir(case_or_attempts_dir: Path) -> Path:
        attempts_dir = (
            case_or_attempts_dir
            if case_or_attempts_dir.name == "attempts"
            else case_or_attempts_dir / "attempts"
        )
        attempts_dir.mkdir(parents=True, exist_ok=True)
        existing = [
            int(item.name)
            for item in attempts_dir.iterdir()
            if item.is_dir() and item.name.isdigit()
        ]
        attempt_dir = attempts_dir / f"{max(existing, default=0) + 1:03d}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        return attempt_dir

    def archive_case_attempt(
        self,
        *,
        attempt_dir: Path,
        report_path: Path | None,
        rows: list[dict[str, Any]],
        sanitized_rows: list[dict[str, Any]],
        patch_keys: tuple[str, ...],
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        archived_patches: list[dict[str, str]] = []
        patches_dir = attempt_dir / "patches"
        patches_dir.mkdir(parents=True, exist_ok=True)
        if report_path is not None and report_path.exists():
            shutil.copy2(report_path, attempt_dir / "report.yml")
        conflict_reports = [
            row.get("conflict_summary")
            for row in rows
            if isinstance(row, dict) and row.get("conflict_summary") is not None
        ]
        if conflict_reports:
            self.write_json(attempt_dir / "conflict-report.json", {"reports": conflict_reports})
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in patch_keys:
                raw = str(row.get(key) or "").strip()
                source = Path(raw).expanduser() if raw else None
                if source is None or not source.is_file():
                    continue
                name = self.safe_slug(
                    f"{key.replace('_path', '')}_{source.name}",
                    fallback=source.name,
                    max_length=160,
                )
                destination = patches_dir / name
                shutil.copy2(source, destination)
                archived_patches.append(
                    {"kind": key, "source": str(source.resolve()), "archive": str(destination)}
                )
        case_result = {
            "attempt_number": int(attempt_dir.name),
            "attempt_dir": str(attempt_dir),
            "report_path": str(report_path) if report_path else "",
            "patches": archived_patches,
            "rows": sanitized_rows,
            "result": result or {},
            "updated_at": self.now_iso(),
        }
        self.write_json(attempt_dir / "attempt.json", case_result)
        self.write_json(attempt_dir.parent.parent / "result.json", case_result)
        return case_result

    def list_case_attempts(self, run_id: str, row_key: str) -> list[dict[str, Any]]:
        run_dir = (self.runs_root / self.safe_slug(run_id)).resolve()
        cases_dirs = [run_dir / "cases"]
        executions_dir = run_dir / "executions"
        if executions_dir.is_dir():
            cases_dirs.extend(
                execution_dir / "cases"
                for execution_dir in executions_dir.iterdir()
                if execution_dir.is_dir()
            )
        safe_row_key = self.safe_slug(row_key, fallback="row", max_length=72)
        case_dirs: list[Path] = []
        for cases_dir in cases_dirs:
            if not cases_dir.is_dir():
                continue
            case_dirs.extend(
                item
                for item in cases_dir.iterdir()
                if item.is_dir()
                and (item.name == safe_row_key or item.name.startswith(f"{safe_row_key}_"))
            )
        attempts: list[dict[str, Any]] = []
        for case_dir in case_dirs:
            attempts_dir = case_dir / "attempts"
            if not attempts_dir.is_dir():
                continue
            for attempt_dir in attempts_dir.iterdir():
                if not attempt_dir.is_dir() or not attempt_dir.name.isdigit():
                    continue
                attempt_path = attempt_dir / "attempt.json"
                if attempt_path.is_file():
                    try:
                        data = json.loads(attempt_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        data = {}
                else:
                    data = {}
                conflict_path = attempt_dir / "conflict-report.json"
                conflict_report: dict[str, Any] | None = None
                if conflict_path.is_file():
                    try:
                        loaded_conflict = json.loads(conflict_path.read_text(encoding="utf-8"))
                        if isinstance(loaded_conflict, dict):
                            conflict_report = loaded_conflict
                    except (json.JSONDecodeError, OSError):
                        conflict_report = None
                report_path = attempt_dir / "report.yml"
                rows = data.get("rows") if isinstance(data.get("rows"), list) else []
                if report_path.is_file() and (not rows or conflict_report is None):
                    try:
                        report_data = yaml.safe_load(report_path.read_text(encoding="utf-8"))
                    except (OSError, yaml.YAMLError):
                        report_data = None
                    report_rows = (
                        report_data.get("commits")
                        if isinstance(report_data, dict) and isinstance(report_data.get("commits"), list)
                        else []
                    )
                    if not rows:
                        rows = [row for row in report_rows if isinstance(row, dict)]
                    if conflict_report is None:
                        reports = [
                            row.get("conflict_summary")
                            for row in report_rows
                            if isinstance(row, dict) and row.get("conflict_summary") is not None
                        ]
                        if reports:
                            conflict_report = {"reports": reports}
                patches = data.get("patches") if isinstance(data.get("patches"), list) else []
                if not patches and (attempt_dir / "patches").is_dir():
                    patches = [
                        {"kind": "archived", "source": "", "archive": str(patch)}
                        for patch in sorted((attempt_dir / "patches").iterdir())
                        if patch.is_file()
                    ]
                execution_dir = self._execution_dir_for(attempt_dir)
                attempts.append(
                    {
                        "execution": int(execution_dir.name)
                        if execution_dir is not None
                        else 0,
                        "attempt_number": int(attempt_dir.name),
                        "attempt_dir": str(attempt_dir),
                        "updated_at": str(data.get("updated_at") or ""),
                        "report_path": str(report_path) if report_path.is_file() else "",
                        "stdout_path": str(attempt_dir / "cvekit.stdout.log")
                        if (attempt_dir / "cvekit.stdout.log").is_file()
                        else "",
                        "stderr_path": str(attempt_dir / "cvekit.stderr.log")
                        if (attempt_dir / "cvekit.stderr.log").is_file()
                        else "",
                        "rows": rows,
                        "patches": patches,
                        "conflict_report": conflict_report,
                    }
                )
        return sorted(
            attempts,
            key=lambda item: (int(item["execution"]), int(item["attempt_number"])),
            reverse=True,
        )

    def artifacts(self, run_dir: Path) -> dict[str, str]:
        run_root = self._run_root_for(run_dir) or run_dir.resolve()
        work_dir = self._execution_dir_for(run_dir) or run_root
        return {
            "run_id": run_root.name,
            "run_dir": str(run_root),
            "execution_dir": str(work_dir),
            "manifest_path": str(run_root / "manifest.json"),
            "run_log_path": str(run_root / "run.log"),
            "input_dir": str(work_dir / "input"),
            "reports_dir": str(work_dir / "reports"),
            "cases_dir": str(work_dir / "cases"),
        }

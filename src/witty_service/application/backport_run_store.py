from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BackportRunStore:
    """Store the small, user-facing Task/Run/Case Backport archive."""

    TERMINAL_STATUSES = {"completed", "completed_with_failures", "failed"}
    ARTIFACT_NAMES = {
        "original_patch": "original.patch",
        "resolved_patch": "run-{run:03d}-resolved.patch",
        "cvekit_log": "run-{run:03d}-cvekit.log",
        "resolution_log": "run-{run:03d}-resolution.log",
        "conflict_report": "run-{run:03d}-conflict-report.json",
    }
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, runs_root: str | Path) -> None:
        self.runs_root = Path(runs_root).expanduser().resolve()
        self._run_id = ""
        self._work_dirs: list[tempfile.TemporaryDirectory[str]] = []
        self._secrets: list[str] = []

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def safe_slug(value: str, *, fallback: str = "item", max_length: int = 96) -> str:
        text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
        return (text or fallback)[:max_length].rstrip("-")

    @classmethod
    def redact(cls, text: str, secrets: list[str] | None = None) -> str:
        value = str(text or "")
        for secret in secrets or []:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        patterns = (
            r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s]+",
            r"(?i)((?:api[_-]?key|openai[_-]?key|token|secret)\s*[=:]\s*)[^\s,;]+",
            r"(?i)(--api-key(?:=|\s+))[^\s]+",
            r"(?i)([?&](?:token|key|secret)=)[^&#\s]+",
        )
        for pattern in patterns:
            value = re.sub(pattern, r"\1[REDACTED]", value)
        return value

    def set_run_id(self, run_id: str | None) -> None:
        self._run_id = self.safe_slug(run_id or "", fallback="")

    def set_secrets(self, secrets: list[str]) -> None:
        self._secrets = [secret for secret in secrets if secret]

    def _lock_for(self, task_dir: Path) -> threading.RLock:
        key = str(task_dir.resolve())
        with self._locks_guard:
            return self._locks.setdefault(key, threading.RLock())

    def _task_root_for(self, path: Path) -> Path | None:
        resolved = path.expanduser().resolve()
        for candidate in (resolved, *resolved.parents):
            if candidate.parent == self.runs_root:
                return candidate
        return None

    def _task_dir(self, task_id: str) -> Path:
        safe_id = self.safe_slug(task_id, fallback="")
        if not safe_id or safe_id != task_id:
            raise ValueError("Invalid Backport task id.")
        task_dir = (self.runs_root / safe_id).resolve()
        if task_dir.parent != self.runs_root:
            raise ValueError("Invalid Backport task path.")
        return task_dir

    def _new_work_dir(self, prefix: str) -> Path:
        holder = tempfile.TemporaryDirectory(prefix=f"witty-backport-{self.safe_slug(prefix)}-")
        self._work_dirs.append(holder)
        return Path(holder.name)

    def cleanup_work_dir(self, path: Path) -> None:
        resolved = path.resolve()
        for holder in list(self._work_dirs):
            if Path(holder.name).resolve() == resolved:
                holder.cleanup()
                self._work_dirs.remove(holder)
                return

    def _read_json(self, path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return dict(default or {})
        return loaded if isinstance(loaded, dict) else dict(default or {})

    def write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)

    def write_json(self, path: Path, data: dict[str, Any]) -> None:
        self.write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str))

    def create_task(
        self,
        *,
        excel_path: str | Path,
        target_repository: str,
        target_branch: str = "",
        target_head: str = "",
        config: dict[str, Any] | None = None,
    ) -> Path:
        excel = Path(excel_path).expanduser()
        repo_name = Path(target_repository.rstrip("/")).name.removesuffix(".git")
        date = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = (
            f"{date}-"
            f"{self.safe_slug(excel.stem, fallback='excel', max_length=40)}-"
            f"{self.safe_slug(repo_name, fallback='repository', max_length=40)}"
        )
        self.runs_root.mkdir(parents=True, exist_ok=True)
        while True:
            task_id = f"{prefix}-{uuid.uuid4().hex[:6]}"
            task_dir = self.runs_root / task_id
            try:
                task_dir.mkdir()
                break
            except FileExistsError:
                continue
        (task_dir / "input").mkdir()
        (task_dir / "runs").mkdir()
        (task_dir / "cases").mkdir()
        shutil.copy2(excel, task_dir / "input" / "source.xlsx")
        if config is not None:
            self.write_json(task_dir / "input" / "config.json", self._sanitize_config(config))
        now = self.now_iso()
        self.write_json(
            task_dir / "task.json",
            {
                "task_id": task_id,
                "name": excel.name,
                "status": "generating",
                "current_run": None,
                "current_report": None,
                "summary": {"total": 0, "success": 0, "failed": 0},
                "target": {
                    "repository": target_repository,
                    "branch": target_branch,
                    "head": target_head,
                },
                "created_at": now,
                "updated_at": now,
            },
        )
        self._run_id = task_id
        return task_dir

    @staticmethod
    def _sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
        denied = {
            "api_key", "apikey", "token", "password", "secret",
            "local_branches", "remote_branches", "warnings", "capabilities",
            "cache_dir", "current_excel_path", "current_report_path",
            "current_filtered_report_path",
        }

        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: clean(item)
                    for key, item in value.items()
                    if key.lower() not in denied
                    and not any(
                        marker in key.lower()
                        for marker in ("api_key", "token", "password", "secret")
                    )
                }
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        return clean(config)

    def read_manifest(self, task_dir_or_id: Path | str) -> dict[str, Any]:
        candidate = Path(task_dir_or_id)
        task_dir = (
            self._task_dir(str(task_dir_or_id))
            if not candidate.is_absolute()
            else self._task_root_for(candidate) or candidate.resolve()
        )
        default = {"task_id": task_dir.name}
        return self._read_json(task_dir / "task.json", default)

    def update_manifest(self, task_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
        task_root = self._task_root_for(task_dir) or task_dir.resolve()
        with self._lock_for(task_root):
            manifest = self.read_manifest(task_root)
            manifest.update(updates)
            manifest["task_id"] = task_root.name
            manifest["updated_at"] = self.now_iso()
            self.write_json(task_root / "task.json", manifest)
            return manifest

    def create_async_record(
        self,
        *,
        run_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if action == "generate_report":
            config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
            target_state = (
                config.get("target_repo_state")
                if isinstance(config.get("target_repo_state"), dict)
                else {}
            )
            excel_path = str(payload.get("excel_path") or payload.get("excelPath") or "")
            target_repository = str(
                config.get("target_repo_input")
                or config.get("target_path")
                or payload.get("target_path")
                or "repository"
            )
            task_dir = self.create_task(
                excel_path=excel_path,
                target_repository=target_repository,
                target_branch=str(
                    target_state.get("selected_branch")
                    or target_state.get("current_branch")
                    or config.get("target_release")
                    or ""
                ),
                target_head=str(
                    target_state.get("head")
                    or target_state.get("target_head")
                    or ""
                ),
                config=config,
            )
            task_id = task_dir.name
            return {
                "run_id": task_id,
                "action": action,
                "status": "running",
                "result": None,
                "error": "",
                "progress": None,
                "pause_requested": False,
                "paused_at": None,
            }

        task_dir = self._task_dir(run_id)
        task = self.read_manifest(task_dir)
        if not (task_dir / "task.json").is_file():
            raise FileNotFoundError(f"Backport task not found: {run_id}")
        current = int(task.get("current_run") or 0)
        current_data = self.read_run(run_id, current) if current else None
        if current_data and current_data.get("status") not in self.TERMINAL_STATUSES:
            if current_data.get("status") not in {"paused", "interrupted"}:
                raise RuntimeError("Backport task already has an active run.")
            number = current
            current_data["status"] = "running"
            current_data["resume_count"] = int(current_data.get("resume_count") or 0) + 1
            current_data.setdefault("events", []).append({"type": "resumed", "at": self.now_iso()})
            current_data.pop("finished_at", None)
        else:
            if task.get("status") in {"generating", "generation_failed"}:
                raise RuntimeError("Backport task is not ready to run.")
            number = current + 1
            target = task.get("target") if isinstance(task.get("target"), dict) else {}
            target_start = {
                "repository": str(target.get("repository") or ""),
                "branch": str(target.get("branch") or ""),
                "head": str(target.get("head") or ""),
                "clean": True,
            }
            current_data = {
                "run": number,
                "status": "pending",
                "resume_count": 0,
                "started_at": self.now_iso(),
                "target_start": target_start,
                "target_end": dict(target_start),
                "summary": {"processed": 0, "success": 0, "failed": 0},
                "cases": [],
                "events": [],
                "report": f"{number:03d}-report.yml",
            }
            source = task_dir / str(task.get("current_report") or "input/initial-report.yml")
            report = task_dir / "runs" / f"{number:03d}-report.yml"
            if not source.is_file():
                raise RuntimeError("Backport task has no initial report.")
            self.write_text(report, source.read_text(encoding="utf-8"))
            current_data["status"] = "running"
        self.write_json(task_dir / "runs" / f"{number:03d}.json", current_data)
        self.update_manifest(
            task_dir,
            {
                "status": "running",
                "current_run": number,
                "current_report": f"runs/{number:03d}-report.yml",
            },
        )
        self._run_id = run_id
        return self.as_async_record(task, current_data, action=action)

    @staticmethod
    def as_async_record(
        task: dict[str, Any],
        run: dict[str, Any] | None = None,
        *,
        action: str = "run_all",
    ) -> dict[str, Any]:
        status = str((run or task).get("status") or "interrupted")
        if status == "ready":
            status = "success"
        return {
            "run_id": str(task.get("task_id") or ""),
            "action": action,
            "status": status,
            "result": None,
            "error": str((run or task).get("error") or ""),
            "progress": None,
            "pause_requested": False,
            "paused_at": None,
        }

    def read_run(self, task_id: str, run_number: int) -> dict[str, Any] | None:
        if run_number < 1:
            return None
        path = self._task_dir(task_id) / "runs" / f"{run_number:03d}.json"
        return self._read_json(path) if path.is_file() else None

    def update_current_execution(self, task_dir: Path, updates: dict[str, Any]) -> None:
        task_root = self._task_root_for(task_dir)
        if task_root is None:
            return
        with self._lock_for(task_root):
            task = self.read_manifest(task_root)
            number = int(task.get("current_run") or 0)
            if not number:
                return
            run = self.read_run(task_root.name, number) or {"run": number}
            status = str(updates.get("status") or "")
            if status == "success":
                status = "completed"
            run.update({
                key: value
                for key, value in updates.items()
                if key not in {"result", "progress", "paused_at"} and value is not None
            })
            if status:
                run["status"] = status
            if status == "paused":
                events = run.setdefault("events", [])
                if not events or events[-1].get("type") != "paused":
                    events.append({"type": "paused", "at": self.now_iso()})
            if status in self.TERMINAL_STATUSES:
                run["finished_at"] = self.now_iso()
            self.write_json(task_root / "runs" / f"{number:03d}.json", run)

    def record_progress(self, task_id: str, progress: dict[str, Any]) -> None:
        task_dir = self._task_dir(task_id)
        with self._lock_for(task_dir):
            task = self.read_manifest(task_dir)
            number = int(task.get("current_run") or 0)
            run = self.read_run(task_id, number)
            if run is None:
                return
            processed = int(progress.get("processed_count") or 0)
            failed = int(progress.get("failed_count") or 0)
            run["summary"] = {
                "processed": processed,
                "success": max(0, processed - failed),
                "failed": failed,
            }
            row_id = str(progress.get("current_row_id") or "").strip()
            if row_id:
                phase = str(progress.get("phase") or "")
                action = {
                    "checking": "check",
                    "resolving": "resolve",
                    "applying": "apply",
                }.get(phase)
                updated = progress.get("updated_commits")
                row = updated[0] if isinstance(updated, list) and updated and isinstance(updated[0], dict) else {}
                case_status = str(row.get("status") or phase or "running").lower()
                commit = str(row.get("commit") or row.get("input_commit") or row_id)
                match = re.search(r"\d+", row_id)
                row_number = int(match.group()) if match else int(progress.get("current_index") or 0)
                commit_slug = re.sub(r"[^0-9a-f]", "", commit.lower())[:12] or self.safe_slug(commit)[:12]
                case_id = f"{row_number:03d}-{commit_slug}"
                cases = run.setdefault("cases", [])
                summary = next(
                    (item for item in cases if isinstance(item, dict) and item.get("id") == case_id),
                    None,
                )
                if summary is None:
                    summary = {"id": case_id, "status": case_status, "actions": [], "message": ""}
                    cases.append(summary)
                summary["status"] = case_status
                summary["message"] = str(progress.get("message") or "")
                if action and action not in summary["actions"]:
                    summary["actions"].append(action)
            self.write_json(task_dir / "runs" / f"{number:03d}.json", run)

    def get_async_record(self, task_id: str, *, active: bool) -> dict[str, Any] | None:
        try:
            task_dir = self._task_dir(task_id)
        except ValueError:
            return None
        if not (task_dir / "task.json").is_file():
            return None
        task = self.read_manifest(task_dir)
        number = int(task.get("current_run") or 0)
        run = self.read_run(task_id, number) if number else None
        if run and run.get("status") == "running" and not active:
            events = run.setdefault("events", [])
            if not any(item.get("type") == "interrupted" for item in events if isinstance(item, dict)):
                events.append({"type": "interrupted", "at": self.now_iso(), "id": "service-restart"})
            run["status"] = "interrupted"
            self.write_json(task_dir / "runs" / f"{number:03d}.json", run)
            task = self.update_manifest(task_dir, {"status": "interrupted"})
        return self.as_async_record(task, run, action="run_all" if run else "generate_report")

    def list_runs(self, *, active_run_ids: set[str] | None = None) -> list[dict[str, Any]]:
        active = active_run_ids or set()
        if not self.runs_root.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for task_dir in self.runs_root.iterdir():
            if not task_dir.is_dir() or not (task_dir / "task.json").is_file():
                continue
            record = self.get_async_record(task_dir.name, active=task_dir.name in active)
            if record is None:
                continue
            task = self.read_manifest(task_dir)
            target = task.get("target") if isinstance(task.get("target"), dict) else {}
            summary = task.get("summary") if isinstance(task.get("summary"), dict) else {}
            record.update(
                {
                    "display_name": str(task.get("name") or task_dir.name),
                    "created_at": str(task.get("created_at") or ""),
                    "updated_at": str(task.get("updated_at") or ""),
                    "current_report_path": str(
                        task_dir / str(task.get("current_report") or "")
                    ) if task.get("current_report") else "",
                    "excel_path": str(task_dir / "input" / "source.xlsx"),
                    "commit_count": int(summary.get("total") or 0),
                    "current_excel_version": 1,
                    "current_execution": int(task.get("current_run") or 0),
                    "run_dir": str(task_dir),
                    "target": target,
                    "summary": summary,
                }
            )
            items.append(record)
        return sorted(items, key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    def list_executions(self, task_id: str) -> list[dict[str, Any]]:
        task_dir = self._task_dir(task_id)
        items: list[dict[str, Any]] = []
        for path in (task_dir / "runs").glob("[0-9][0-9][0-9].json"):
            run = self._read_json(path)
            items.append(
                {
                    "execution": int(run.get("run") or path.stem),
                    "status": str(run.get("status") or "interrupted"),
                    "action": "run_all",
                    "created_at": str(run.get("started_at") or ""),
                    "updated_at": str(run.get("finished_at") or run.get("started_at") or ""),
                    "report_path": str(task_dir / "runs" / str(run.get("report") or "")),
                    "target": run.get("target_end") or run.get("target_start") or {},
                    "excel_version": 1,
                }
            )
        return sorted(items, key=lambda item: int(item["execution"]), reverse=True)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            task_dir = self._task_dir(task_id)
        except ValueError:
            return None
        if not (task_dir / "task.json").is_file():
            return None
        task = self.read_manifest(task_dir)
        task["runs"] = [
            self._read_json(path)
            for path in sorted((task_dir / "runs").glob("[0-9][0-9][0-9].json"))
        ]
        task["cases"] = [
            self._read_json(path)
            for path in sorted((task_dir / "cases").glob("*/case.json"))
            if not path.parent.is_symlink()
        ]
        return task

    def get_case(self, task_id: str, case_id: str) -> dict[str, Any] | None:
        task_dir = self._task_dir(task_id)
        safe_case_id = self.safe_slug(case_id, fallback="")
        if not safe_case_id or safe_case_id != case_id:
            return None
        case_dir = (task_dir / "cases" / safe_case_id).resolve()
        if case_dir.parent != (task_dir / "cases").resolve() or case_dir.is_symlink():
            return None
        path = case_dir / "case.json"
        return self._read_json(path) if path.is_file() else None

    def read_report(self, task_id: str, run_number: int | None = None) -> tuple[str, str]:
        task_dir = self._task_dir(task_id)
        if run_number is None:
            path = task_dir / "input" / "initial-report.yml"
        else:
            if run_number < 1:
                raise ValueError("Invalid run number.")
            path = task_dir / "runs" / f"{run_number:03d}-report.yml"
        if not path.is_file() or path.is_symlink() or self._task_root_for(path) != task_dir:
            raise FileNotFoundError("Backport report not found.")
        return path.name, path.read_text(encoding="utf-8")

    def is_report_frozen(self, path: Path) -> bool:
        task_dir = self._task_root_for(path)
        match = re.fullmatch(r"(\d{3})-report\.yml", path.name)
        if task_dir is None or match is None or path.parent != task_dir / "runs":
            return False
        run = self.read_run(task_dir.name, int(match.group(1)))
        return bool(run and run.get("status") in self.TERMINAL_STATUSES)

    def create_run_dir(self, *, operation: str, request: dict[str, Any] | None = None) -> Path:
        if operation == "generate_report":
            if self._run_id:
                task_dir = self._task_dir(self._run_id)
            else:
                request = request or {}
                task_dir = self.create_task(
                    excel_path=str(request.get("excel_path") or ""),
                    target_repository=str(request.get("target_path") or "repository"),
                    target_branch=str(request.get("target_release") or ""),
                    config=request,
                )
            return self._new_work_dir(task_dir.name)
        return self._new_work_dir(operation)

    def archive_excel(self, run_dir: Path, excel_path: Path) -> int:
        if self._run_id:
            task_dir = self._task_dir(self._run_id)
            destination = task_dir / "input" / "source.xlsx"
            if excel_path.resolve() != destination.resolve():
                shutil.copy2(excel_path, destination)
        return 1

    def ensure_for_report(self, report_path: Path) -> Path:
        task_root = self._task_root_for(report_path)
        if task_root is not None:
            return task_root
        if self._run_id:
            return self._task_dir(self._run_id)
        raise ValueError("Report is outside the Backport archive.")

    def next_attempt_dir(self, case_or_attempts_dir: Path) -> Path:
        return self._new_work_dir(case_or_attempts_dir.name)

    def case_dir(
        self,
        task_dir: Path,
        row: dict[str, Any],
        *,
        row_id: str,
    ) -> Path:
        task_root = self._task_root_for(task_dir) or self._task_dir(self._run_id)
        raw_row = row.get("row") or row.get("row_number") or row.get("index") or row_id
        match = re.search(r"\d+", str(raw_row))
        number = int(match.group()) if match else 0
        commit = str(
            row.get("commit")
            or row.get("commit_id")
            or row.get("input_commit")
            or row.get("upstream_commit")
            or row_id
        )
        commit_slug = re.sub(r"[^0-9a-f]", "", commit.lower())[:12] or self.safe_slug(commit)[:12]
        case_id = f"{number:03d}-{commit_slug}"
        case_dir = task_root / "cases" / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        case_path = case_dir / "case.json"
        if not case_path.is_file():
            self.write_json(
                case_path,
                {
                    "case_id": case_id,
                    "row": number,
                    "commit": commit,
                    "title": str(row.get("title") or row.get("subject") or ""),
                    "status": "pending",
                    "applied_commit": None,
                    "last_run": None,
                    "artifacts": {},
                    "runs": {},
                    "updated_at": self.now_iso(),
                },
            )
        return case_dir

    def write_command_logs(
        self,
        *,
        cwd: Path,
        command: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        del command, returncode, stdout
        cleaned = self.redact(stderr, self._secrets).strip()
        if cleaned:
            if self._run_id:
                task_dir = self._task_dir(self._run_id)
                if self.read_manifest(task_dir).get("status") == "generating":
                    self.write_text(task_dir / "input" / "cvekit.log", cleaned + "\n")
                    return
            self.write_text(cwd / "cvekit.stderr.log", cleaned + "\n")

    def archive_case_attempt(
        self,
        *,
        attempt_dir: Path,
        report_path: Path,
        rows: list[dict[str, Any]],
        sanitized_rows: list[dict[str, Any]],
        patch_keys: tuple[str, ...] | list[str],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        del report_path
        if not rows or not self._run_id:
            return {}
        task_dir = self._task_dir(self._run_id)
        case_dir = self.case_dir(task_dir, rows[0], row_id=str(rows[0].get("row_id") or "0"))
        task = self.read_manifest(task_dir)
        run_number = int(task.get("current_run") or 0)
        run_key = f"{run_number:03d}"
        row = sanitized_rows[0] if sanitized_rows else rows[0]
        status = str(row.get("status") or result.get("status") or "failed").lower()
        artifacts: dict[str, str] = {}

        original_source = next(
            (
                Path(str(rows[0].get(key))).expanduser()
                for key in patch_keys
                if "original" in key and rows[0].get(key) and Path(str(rows[0].get(key))).expanduser().is_file()
            ),
            None,
        )
        if original_source is not None and not (case_dir / "original.patch").is_file():
            shutil.copy2(original_source, case_dir / "original.patch")
        if (case_dir / "original.patch").is_file():
            artifacts["original_patch"] = "original.patch"

        resolved_source = next(
            (
                Path(str(rows[0].get(key))).expanduser()
                for key in patch_keys
                if "backport" in key and rows[0].get(key) and Path(str(rows[0].get(key))).expanduser().is_file()
            ),
            None,
        )
        if resolved_source is not None:
            original_bytes = (case_dir / "original.patch").read_bytes() if (case_dir / "original.patch").is_file() else None
            resolved_bytes = resolved_source.read_bytes()
            if resolved_bytes and resolved_bytes != original_bytes:
                name = self.ARTIFACT_NAMES["resolved_patch"].format(run=run_number)
                shutil.copy2(resolved_source, case_dir / name)
                artifacts["resolved_patch"] = name

        stderr_path = attempt_dir / "cvekit.stderr.log"
        if stderr_path.is_file():
            cleaned = self.redact(
                stderr_path.read_text(encoding="utf-8"),
                self._secrets,
            ).strip()
            if cleaned:
                name = self.ARTIFACT_NAMES["cvekit_log"].format(run=run_number)
                self.write_text(case_dir / name, cleaned + "\n")
                artifacts["cvekit_log"] = name

        conflict_source = attempt_dir / "conflict-report.json"
        if conflict_source.is_file():
            name = self.ARTIFACT_NAMES["conflict_report"].format(run=run_number)
            self.write_text(
                case_dir / name,
                self.redact(
                    conflict_source.read_text(encoding="utf-8"),
                    self._secrets,
                ),
            )
            artifacts["conflict_report"] = name
        elif rows[0].get("conflict_summary") is not None:
            name = self.ARTIFACT_NAMES["conflict_report"].format(run=run_number)
            self.write_text(
                case_dir / name,
                self.redact(
                    json.dumps(
                        rows[0]["conflict_summary"],
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    self._secrets,
                ),
            )
            artifacts["conflict_report"] = name

        case = self._read_json(case_dir / "case.json")
        case.update(
            {
                "status": status,
                "applied_commit": row.get("applied_commit"),
                "last_run": run_number,
                "updated_at": self.now_iso(),
            }
        )
        case.setdefault("artifacts", {}).update(
            {"original_patch": "original.patch"} if (case_dir / "original.patch").is_file() else {}
        )
        case.setdefault("runs", {})[run_key] = {
            "status": status,
            "applied_commit": row.get("applied_commit"),
            "artifacts": artifacts,
        }
        self.write_json(case_dir / "case.json", case)
        self.cleanup_work_dir(attempt_dir)
        return {"case_id": case_dir.name, "artifacts": artifacts}

    def list_case_attempts(self, task_id: str, row_key: str) -> list[dict[str, Any]]:
        task_dir = self._task_dir(task_id)
        matches = [
            path for path in (task_dir / "cases").iterdir()
            if path.is_dir() and (path.name == row_key or path.name.startswith(f"{int(row_key):03d}-"))
        ] if (task_dir / "cases").is_dir() else []
        items: list[dict[str, Any]] = []
        for case_dir in matches:
            case = self._read_json(case_dir / "case.json")
            for run_key, data in (case.get("runs") or {}).items():
                artifacts = data.get("artifacts") if isinstance(data, dict) else {}
                items.append(
                    {
                        "execution": int(run_key),
                        "attempt_number": 1,
                        "attempt_dir": str(case_dir),
                        "updated_at": str(case.get("updated_at") or ""),
                        "report_path": "",
                        "stdout_path": "",
                        "stderr_path": str(case_dir / artifacts["cvekit_log"])
                        if artifacts.get("cvekit_log") else "",
                        "rows": [],
                        "patches": [
                            {"kind": kind, "source": "", "archive": str(case_dir / name)}
                            for kind, name in artifacts.items() if name.endswith(".patch")
                        ],
                        "conflict_report": self._read_json(case_dir / artifacts["conflict_report"])
                        if artifacts.get("conflict_report") else None,
                    }
                )
        return sorted(items, key=lambda item: int(item["execution"]), reverse=True)

    def read_artifact(self, task_id: str, case_id: str, artifact: str) -> tuple[Path, bytes]:
        task_dir = self._task_dir(task_id)
        case_dir = (task_dir / "cases" / self.safe_slug(case_id)).resolve()
        if case_dir.parent != (task_dir / "cases").resolve() or case_dir.is_symlink():
            raise ValueError("Invalid case path.")
        case = self._read_json(case_dir / "case.json")
        registered = set((case.get("artifacts") or {}).values())
        for run in (case.get("runs") or {}).values():
            if isinstance(run, dict):
                registered.update((run.get("artifacts") or {}).values())
        if artifact not in registered or artifact != Path(artifact).name:
            raise ValueError("Artifact is not registered.")
        path = case_dir / artifact
        if not path.is_file() or path.is_symlink() or path.resolve().parent != case_dir:
            raise ValueError("Invalid artifact path.")
        return path, path.read_bytes()

    def artifacts(self, task_dir: Path) -> dict[str, str]:
        task_root = self._task_root_for(task_dir) or self._task_dir(self._run_id)
        task = self.read_manifest(task_root)
        return {
            "run_id": task_root.name,
            "run_dir": str(task_root),
            "manifest_path": str(task_root / "task.json"),
            "input_dir": str(task_root / "input"),
            "reports_dir": str(task_root / "runs"),
            "cases_dir": str(task_root / "cases"),
            "report_path": str(task_root / str(task.get("current_report") or ""))
            if task.get("current_report") else "",
        }

    def append_run_log(self, task_dir: Path, message: str) -> None:
        del task_dir, message

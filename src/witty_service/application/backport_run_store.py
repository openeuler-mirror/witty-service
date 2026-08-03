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

import yaml


class BackportRunStore:
    """Store the small, user-facing Task/Run/Case Backport archive."""

    TERMINAL_STATUSES = {"completed", "completed_with_failures", "failed"}
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
                masked = f"{secret[:6]}[REDACTED]" if len(secret) > 6 else "[REDACTED]"
                value = value.replace(secret, masked)
        api_key_patterns = (
            r"(?i)((?:api[_-]?key|openai[_-]?key)\s*[=:]\s*)([^\s,;]+)",
            r"(?i)(--api-key(?:=|\s+))([^\s]+)",
        )
        for pattern in api_key_patterns:
            value = re.sub(
                pattern,
                lambda match: (
                    match.group(1)
                    + (
                        match.group(2)
                        if "[REDACTED]" in match.group(2)
                        else (
                            f"{match.group(2)[:6]}[REDACTED]"
                            if len(match.group(2)) > 6
                            else "[REDACTED]"
                        )
                    )
                ),
                value,
            )
        patterns = (
            r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s]+",
            r"(?i)((?:token|password|secret)\s*[=:]\s*)[^\s,;]+",
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

    @staticmethod
    def _summary_text(value: Any, *, limit: int = 2000) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"

    @staticmethod
    def _report_rows(path: Path) -> list[dict[str, Any]]:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return []
        rows = loaded.get("commits") if isinstance(loaded, dict) else None
        return [dict(row) for row in rows or [] if isinstance(row, dict)]

    @classmethod
    def _summary_case(cls, row: dict[str, Any], index: int) -> dict[str, Any]:
        commit = str(row.get("commit") or row.get("input_commit") or row.get("row_id") or "")
        row_number = int(row.get("row_number") or index)
        title = cls._summary_text(
            row.get("commit_title") or row.get("title") or row.get("subject") or ""
        )
        commit_slug = re.sub(r"[^0-9a-f]", "", commit.lower())[:12]
        case = {
            "id": f"{row_number:03d}-{commit_slug or cls.safe_slug(commit)[:12]}",
            "row": row_number,
            "commit": commit,
            "title": title,
            "detection": {"state": "not_started", "result": ""},
            "handling": {
                "state": "not_started",
                "result": "",
                "engine": "",
                "report": "",
            },
            "final": {
                "state": "not_started",
                "result": "",
                "applied_commit": "",
            },
        }
        cls._set_detection_from_row(case, row)
        return case

    @staticmethod
    def _set_detection_from_row(
        case: dict[str, Any],
        row: dict[str, Any],
    ) -> None:
        detection = case["detection"]
        if detection.get("state") in {"completed", "failed"}:
            return
        if row.get("equivalent_exists") is True:
            detection.update({"state": "completed", "result": "equivalent_exists"})
        elif str(row.get("applied_commit") or "").strip():
            applied_patch_kind = str(row.get("applied_patch_kind") or "").strip().lower()
            has_backported_patch = bool(
                str(row.get("backported_patch_path") or "").strip()
            )
            used_resolved_patch = (
                applied_patch_kind not in {"", "original"}
                or (has_backported_patch and applied_patch_kind != "original")
            )
            detection.update(
                {
                    "state": "completed",
                    "result": "conflict" if used_resolved_patch else "clean_apply",
                }
            )
        elif row.get("merged_in_target") is True:
            detection.update({"state": "completed", "result": "already_present"})
        elif row.get("has_conflict") is True:
            detection.update({"state": "completed", "result": "conflict"})
        elif str(row.get("status") or "").lower() in {"failed", "error"}:
            detection.update({"state": "failed", "result": "failed"})
        elif row.get("has_conflict") is False:
            detection.update({"state": "completed", "result": "clean_apply"})

    @classmethod
    def _update_summary_case(
        cls,
        case: dict[str, Any],
        row: dict[str, Any],
        *,
        phase: str,
        phase_state: str,
    ) -> None:
        status = str(row.get("status") or "").strip().lower()
        detection = case["detection"]
        handling = case["handling"]
        final = case["final"]

        if phase == "checking":
            if phase_state == "running" and detection["state"] == "not_started":
                detection["state"] = "running"
            elif phase_state == "completed":
                cls._set_detection_from_row(case, row)
            elif phase_state == "failed":
                detection.update({"state": "failed", "result": "failed"})

        elif phase == "resolving":
            cls._set_detection_from_row(case, row)
            if detection["state"] == "not_started":
                detection.update({"state": "completed", "result": "conflict"})
            handling["engine"] = cls._summary_text(row.get("backport_engine"), limit=80)
            report = row.get("backport_explanation")
            if not report and isinstance(row.get("conflict_summary"), dict):
                report = (
                    row["conflict_summary"].get("reason")
                    or row["conflict_summary"].get("error")
                )
            if report:
                handling["report"] = cls._summary_text(report)
            if phase_state == "running":
                handling["state"] = "running"
            elif phase_state == "failed" or status in {"failed", "error"}:
                handling.update({"state": "failed", "result": "failed"})
            elif row.get("equivalent_exists") is True:
                handling.update({"state": "completed", "result": "equivalent_exists"})
            elif str(row.get("backported_patch_path") or "").strip():
                handling.update({"state": "completed", "result": "backport_generated"})
                final.update({"state": "not_started", "result": "ready_to_apply"})

        elif phase == "applying":
            if phase_state == "running":
                final.update({"state": "running", "result": ""})
            elif phase_state == "completed":
                cls._set_detection_from_row(case, row)
            applied_commit = cls._summary_text(row.get("applied_commit"), limit=80)
            if (
                phase_state == "completed"
                and applied_commit
                and detection["state"] in {"not_started", "running"}
            ):
                detection.update({"state": "completed", "result": "clean_apply"})
            if (
                phase_state == "completed"
                and detection.get("result") == "clean_apply"
                and handling["state"] == "not_started"
            ):
                handling.update({"state": "completed", "result": "direct_apply"})
            elif (
                phase_state == "completed"
                and detection.get("result") == "conflict"
                and handling["state"] == "not_started"
                and str(row.get("backported_patch_path") or "").strip()
            ):
                handling.update({"state": "completed", "result": "backport_generated"})
                handling["engine"] = cls._summary_text(
                    row.get("backport_engine"),
                    limit=80,
                )
            if phase_state == "completed" and applied_commit:
                final.update(
                    {
                        "state": "completed",
                        "result": "applied",
                        "applied_commit": applied_commit,
                    }
                )
            elif phase_state == "failed" or status in {"failed", "error"}:
                final.update({"state": "failed", "result": "failed"})

        elif phase == "skipped":
            cls._set_detection_from_row(case, row)
            if detection.get("result") in {"equivalent_exists", "already_present"}:
                handling.update(
                    {
                        "state": "not_needed",
                        "result": "equivalent_exists",
                    }
                )
            applied_commit = cls._summary_text(row.get("applied_commit"), limit=80)
            if applied_commit:
                final.update(
                    {
                        "state": "completed",
                        "result": "applied",
                        "applied_commit": applied_commit,
                    }
                )
            else:
                final.update({"state": "completed", "result": "skipped"})

        elif phase == "failed":
            cls._set_detection_from_row(case, row)
            if detection["state"] in {"not_started", "running"}:
                detection.update({"state": "failed", "result": "failed"})
            elif detection.get("result") == "conflict" and handling["state"] in {
                "not_started",
                "running",
            }:
                handling.update({"state": "failed", "result": "failed"})
            else:
                final.update({"state": "failed", "result": "failed"})

        report = row.get("backport_explanation")
        if report:
            handling["report"] = cls._summary_text(report)
        if row.get("backport_engine"):
            handling["engine"] = cls._summary_text(row.get("backport_engine"), limit=80)

    @staticmethod
    def _summary_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
        applied_cases = [
            case for case in cases
            if case["final"].get("result") == "applied"
        ]
        conflict_resolved = sum(
            case["detection"].get("result") == "conflict"
            for case in applied_cases
        )
        failed_cases = [
            case for case in cases
            if case["final"].get("result") != "applied"
            and any(
                stage.get("state") == "failed"
                for stage in (
                    case["detection"],
                    case["handling"],
                    case["final"],
                )
            )
        ]
        equivalent_cases = [
            case for case in cases
            if case["final"].get("result") != "applied"
            and case not in failed_cases
            and (
                case["detection"].get("result")
                in {"equivalent_exists", "already_present"}
                or case["handling"].get("result") == "equivalent_exists"
            )
        ]
        classified = len(applied_cases) + len(failed_cases) + len(equivalent_cases)
        return {
            "total": len(cases),
            "applied": len(applied_cases),
            "direct_applied": len(applied_cases) - conflict_resolved,
            "conflict_resolved": conflict_resolved,
            "equivalent_exists": len(equivalent_cases),
            "failed": len(failed_cases),
            "unprocessed": max(0, len(cases) - classified),
        }

    @classmethod
    def _summary_markdown(cls, run: dict[str, Any]) -> str:
        run_number = int(run.get("run") or 0)
        status_labels = {
            "pending": "等待运行",
            "running": "运行中",
            "paused": "已暂停",
            "interrupted": "已中断",
            "completed": "运行完成",
            "completed_with_failures": "运行完成，存在失败",
            "failed": "运行失败",
        }
        detection_labels = {
            "clean_apply": "无冲突，可直接应用",
            "conflict": "存在冲突",
            "equivalent_exists": "等价已存在",
            "already_present": "目标分支已包含",
            "failed": "检测失败",
        }
        final_labels = {
            "applied": "已应用",
            "skipped": "已跳过",
            "ready_to_apply": "等待应用",
            "failed": "失败",
        }
        cases = [
            case for case in run.get("cases") or [] if isinstance(case, dict)
        ]
        counts = cls._summary_counts(cases)
        target = (
            run.get("target_end")
            if isinstance(run.get("target_end"), dict)
            else run.get("target_start")
            if isinstance(run.get("target_start"), dict)
            else {}
        )
        lines = [
            f"# Backport 一键运行报告 {run_number:03d}",
            "",
            f"- 状态：{status_labels.get(str(run.get('status') or ''), str(run.get('status') or '未知'))}",
            f"- 目标分支：{cls._summary_text(target.get('branch')) or '--'}",
            f"- Commit：{counts['total']}",
            f"- 已应用：{counts['applied']}",
            f"  - 直接应用：{counts['direct_applied']}",
            f"  - 解冲突后应用：{counts['conflict_resolved']}",
            f"- 等价存在：{counts['equivalent_exists']}",
            f"- 失败：{counts['failed']}",
            f"- 未处理：{counts['unprocessed']}",
        ]
        for case in sorted(cases, key=lambda item: int(item.get("row") or 0)):
            row = int(case.get("row") or 0)
            commit = cls._summary_text(case.get("commit"), limit=80)[:12] or "--"
            title = cls._summary_text(case.get("title"))
            lines.extend(["", f"## #{row} {commit} {title}".rstrip(), ""])
            detection = case["detection"]
            if detection.get("state") in {"not_started", "running"}:
                state_label = (
                    "检测中" if detection.get("state") == "running" else "尚未检测"
                )
                lines.append(f"- 检测状态：{state_label}")
                continue
            lines.append(
                f"- 检测结论：{detection_labels.get(detection.get('result'), '检测失败')}"
            )
            final = case["final"]
            final_label = final_labels.get(final.get("result"))
            if not final_label:
                final_label = (
                    "应用中"
                    if final.get("state") == "running"
                    else "尚未完成"
                )
            applied_commit = cls._summary_text(
                final.get("applied_commit"),
                limit=80,
            )
            if applied_commit and final.get("result") == "applied":
                final_label = f"{final_label}（{applied_commit[:12]}）"
            lines.append(f"- 最终结果：{final_label}")
            handling = case["handling"]
            report = cls._summary_text(handling.get("report"))
            if report:
                lines.append(f"- 解冲突报告：{report}")
        return "\n".join(lines).rstrip() + "\n"

    def _execution_summary(
        self,
        task_dir: Path,
        run: dict[str, Any],
    ) -> dict[str, Any] | None:
        summary_name = str(run.get("summary_report") or "")
        if not summary_name:
            return None
        cases = [
            case for case in run.get("cases") or [] if isinstance(case, dict)
        ]
        return {
            "path": str(task_dir / "runs" / summary_name),
            "status": str(run.get("status") or ""),
            "counts": self._summary_counts(cases),
            "cases": cases,
        }

    def _write_run_summary(
        self,
        task_dir: Path,
        run: dict[str, Any],
    ) -> dict[str, Any] | None:
        summary = self._execution_summary(task_dir, run)
        if summary is None:
            return None
        self.write_text(Path(summary["path"]), self._summary_markdown(run))
        return summary

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
                "summary_report": f"{number:03d}-summary.md",
            }
            source = task_dir / str(task.get("current_report") or "input/initial-report.yml")
            report = task_dir / "runs" / f"{number:03d}-report.yml"
            if not source.is_file():
                raise RuntimeError("Backport task has no initial report.")
            self.write_text(report, source.read_text(encoding="utf-8"))
            current_data["cases"] = [
                self._summary_case(row, index)
                for index, row in enumerate(self._report_rows(report), start=1)
            ]
            current_data["status"] = "running"
        self.write_json(task_dir / "runs" / f"{number:03d}.json", current_data)
        self._write_run_summary(task_dir, current_data)
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

    def as_async_record(
        self,
        task: dict[str, Any],
        run: dict[str, Any] | None = None,
        *,
        action: str = "run_all",
    ) -> dict[str, Any]:
        status = str((run or task).get("status") or "interrupted")
        if status == "ready":
            status = "success"
        record = {
            "run_id": str(task.get("task_id") or ""),
            "action": action,
            "status": status,
            "result": None,
            "error": str((run or task).get("error") or ""),
            "progress": None,
            "pause_requested": False,
            "paused_at": None,
        }
        task_id = str(task.get("task_id") or "")
        if task_id and run:
            record["execution_summary"] = self._execution_summary(
                self._task_dir(task_id),
                run,
            )
        return record

    def read_run(self, task_id: str, run_number: int) -> dict[str, Any] | None:
        if run_number < 1:
            return None
        path = self._task_dir(task_id) / "runs" / f"{run_number:03d}.json"
        return self._read_json(path) if path.is_file() else None

    def update_current_execution(
        self,
        task_dir: Path,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        task_root = self._task_root_for(task_dir)
        if task_root is None:
            return None
        with self._lock_for(task_root):
            task = self.read_manifest(task_root)
            number = int(task.get("current_run") or 0)
            if not number:
                return None
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
            return self._write_run_summary(task_root, run)

    def record_progress(
        self,
        task_id: str,
        progress: dict[str, Any],
    ) -> dict[str, Any] | None:
        task_dir = self._task_dir(task_id)
        with self._lock_for(task_dir):
            task = self.read_manifest(task_dir)
            number = int(task.get("current_run") or 0)
            run = self.read_run(task_id, number)
            if run is None:
                return None
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
                phase_state = str(progress.get("phase_state") or "")
                updated = progress.get("updated_commits")
                row = updated[0] if isinstance(updated, list) and updated and isinstance(updated[0], dict) else {}
                commit = str(row.get("commit") or row.get("input_commit") or row_id)
                row_number = int(row.get("row_number") or progress.get("current_index") or 0)
                cases = run.setdefault("cases", [])
                summary = next(
                    (
                        item
                        for item in cases
                        if isinstance(item, dict)
                        and (
                            item.get("row") == row_number
                            or (
                                commit
                                and str(item.get("commit") or "").lower()
                                == commit.lower()
                            )
                        )
                    ),
                    None,
                )
                if summary is None:
                    summary = self._summary_case(
                        row,
                        row_number or len(cases) + 1,
                    )
                    cases.append(summary)
                summary["commit"] = commit or summary.get("commit", "")
                if row.get("commit_title") or row.get("title") or row.get("subject"):
                    summary["title"] = self._summary_text(
                        row.get("commit_title")
                        or row.get("title")
                        or row.get("subject")
                    )
                self._update_summary_case(
                    summary,
                    row,
                    phase=phase,
                    phase_state=phase_state,
                )
            self.write_json(task_dir / "runs" / f"{number:03d}.json", run)
            return self._write_run_summary(task_dir, run)

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
            self._write_run_summary(task_dir, run)
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
                    "execution_summary": self._execution_summary(task_dir, run),
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

    @staticmethod
    def _available_artifact_name(case_dir: Path, requested_name: str) -> str:
        requested = case_dir / requested_name
        if not requested.exists():
            return requested_name
        for attempt_number in range(2, 1000):
            candidate = (
                f"{requested.stem}-attempt-{attempt_number:03d}{requested.suffix}"
            )
            if not (case_dir / candidate).exists():
                return candidate
        raise RuntimeError(f"Backport case artifact attempts exhausted: {requested_name}")

    def case_dir(
        self,
        task_dir: Path,
        row: dict[str, Any],
        *,
        row_id: str,
    ) -> Path:
        task_root = self._task_root_for(task_dir) or self._task_dir(self._run_id)
        raw_row = row.get("row_number") or row.get("row") or row.get("index")
        number = int(raw_row) if str(raw_row or "").isdigit() else 0
        commit = str(
            row.get("commit")
            or row.get("commit_id")
            or row.get("input_commit")
            or row.get("upstream_commit")
            or row_id
        )
        commit_slug = re.sub(r"[^0-9a-f]", "", commit.lower())[:12] or self.safe_slug(commit)[:12]
        for existing in (task_root / "cases").glob("*/case.json"):
            existing_case = self._read_json(existing)
            existing_commit = str(existing_case.get("commit") or "")
            if existing_commit and (
                existing_commit.lower().startswith(commit.lower())
                or commit.lower().startswith(existing_commit.lower())
            ):
                return existing.parent
        title = str(
            row.get("commit_title")
            or row.get("title")
            or row.get("subject")
            or ""
        )
        title_slug = self.safe_slug(title, fallback="commit", max_length=48)
        case_id = f"{number:03d}-{commit_slug}-{title_slug}"
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
                    "title": title,
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
    ) -> Path:
        del command, returncode, stdout
        archive_dir = cwd
        if self._run_id:
            task_dir = self._task_dir(self._run_id)
            if self.read_manifest(task_dir).get("status") == "generating":
                archive_dir = task_dir / "input"
        cleaned = self.redact(stderr, self._secrets).strip()
        if cleaned:
            log_name = "cvekit.log" if archive_dir != cwd else "cvekit.stderr.log"
            self.write_text(archive_dir / log_name, cleaned + "\n")
        return archive_dir

    def archive_case_attempt(
        self,
        *,
        attempt_dir: Path,
        report_path: Path,
        rows: list[dict[str, Any]],
        sanitized_rows: list[dict[str, Any]],
        patch_keys: tuple[str, ...] | list[str],
        result: dict[str, Any],
        cleanup: bool = True,
        task_dir: Path | None = None,
        archive_scope: str = "run",
    ) -> dict[str, Any]:
        del report_path
        if not rows:
            raise ValueError("Backport case archive requires at least one row.")
        if archive_scope not in {"run", "interaction"}:
            raise ValueError(f"Invalid Backport archive scope: {archive_scope}")

        task_root = self._task_root_for(task_dir) if task_dir is not None else None
        if task_root is None and self._run_id:
            task_root = self._task_dir(self._run_id)
        if task_root is None or not (task_root / "task.json").is_file():
            raise RuntimeError("Backport case archive cannot resolve its Task.")

        with self._lock_for(task_root):
            case_dir = self.case_dir(
                task_root,
                rows[0],
                row_id=str(rows[0].get("row_id") or "0"),
            )
            case = self._read_json(case_dir / "case.json")
            task = self.read_manifest(task_root)
            if archive_scope == "run":
                archive_number = int(task.get("current_run") or 0)
                if archive_number < 1:
                    raise RuntimeError(
                        "Backport Run archive requires a current one-click Run."
                    )
                archive_collection = "runs"
                last_archive_field = "last_run"
            else:
                existing_numbers = [
                    int(key)
                    for key in (case.get("interactions") or {})
                    if str(key).isdigit()
                ]
                archive_number = max(
                    [int(case.get("last_interaction") or 0), *existing_numbers]
                ) + 1
                archive_collection = "interactions"
                last_archive_field = "last_interaction"

            archive_key = f"{archive_number:03d}"
            artifact_prefix = f"{archive_scope}-{archive_key}"
            row = sanitized_rows[0] if sanitized_rows else rows[0]
            status = str(
                row.get("status") or result.get("status") or "failed"
            ).lower()
            artifacts: dict[str, str] = {}

            original_source = next(
                (
                    Path(str(rows[0].get(key))).expanduser()
                    for key in patch_keys
                    if "original" in key
                    and rows[0].get(key)
                    and Path(str(rows[0].get(key))).expanduser().is_file()
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
                    if "backport" in key
                    and rows[0].get(key)
                    and Path(str(rows[0].get(key))).expanduser().is_file()
                ),
                None,
            )
            if resolved_source is not None:
                original_bytes = (
                    (case_dir / "original.patch").read_bytes()
                    if (case_dir / "original.patch").is_file()
                    else None
                )
                resolved_bytes = resolved_source.read_bytes()
                if resolved_bytes and resolved_bytes != original_bytes:
                    name = self._available_artifact_name(
                        case_dir,
                        f"{artifact_prefix}-resolved.patch",
                    )
                    shutil.copy2(resolved_source, case_dir / name)
                    artifacts["resolved_patch"] = name

            operation = str(result.get("operation") or "")
            log_source_value = result.get("batch_logfile") or rows[0].get(
                "batch_logfile"
            )
            log_source = (
                Path(str(log_source_value)).expanduser()
                if log_source_value
                else None
            )
            if log_source is not None and not log_source.is_file():
                archived_source = attempt_dir / log_source.name
                if archived_source.is_file():
                    log_source = archived_source
            if log_source is not None and log_source.is_file():
                cleaned = self.redact(
                    log_source.read_text(encoding="utf-8"),
                    self._secrets,
                ).strip()
                if cleaned:
                    if operation == "apply_row":
                        name = f"{artifact_prefix}-apply.log"
                    else:
                        engine = str(
                            rows[0].get("backport_engine") or ""
                        ).strip().lower()
                        invoked_value = rows[0].get("backport_engine_invoked")
                        used_engine = (
                            bool(rows[0].get("logfile"))
                            if invoked_value is None
                            else bool(invoked_value)
                        )
                        suffix = (
                            f"-{self.safe_slug(engine)}"
                            if engine and used_engine
                            else ""
                        )
                        name = f"{artifact_prefix}-backport{suffix}.log"
                    name = self._available_artifact_name(case_dir, name)
                    self.write_text(case_dir / name, cleaned + "\n")
                    artifacts[
                        "apply_log" if operation == "apply_row" else "backport_log"
                    ] = name

            engine_log_value = rows[0].get("logfile")
            engine_log = (
                Path(str(engine_log_value)).expanduser()
                if engine_log_value
                else None
            )
            if engine_log is not None and not engine_log.is_file():
                archived_engine_log = attempt_dir / engine_log.name
                if archived_engine_log.is_file():
                    engine_log = archived_engine_log
            if (
                engine_log is not None
                and engine_log.is_file()
                and engine_log != log_source
            ):
                engine = self.safe_slug(
                    str(rows[0].get("backport_engine") or "engine"),
                    fallback="engine",
                )
                name = self._available_artifact_name(
                    case_dir,
                    f"{artifact_prefix}-{engine}.log",
                )
                cleaned = self.redact(
                    engine_log.read_text(encoding="utf-8", errors="replace"),
                    self._secrets,
                ).strip()
                if cleaned:
                    self.write_text(case_dir / name, cleaned + "\n")
                    artifacts["engine_log"] = name

            conflict_source = attempt_dir / "conflict-report.json"
            if conflict_source.is_file():
                name = self._available_artifact_name(
                    case_dir,
                    f"{artifact_prefix}-conflict-report.json",
                )
                self.write_text(
                    case_dir / name,
                    self.redact(
                        conflict_source.read_text(encoding="utf-8"),
                        self._secrets,
                    ),
                )
                artifacts["conflict_report"] = name
            elif rows[0].get("conflict_summary") is not None:
                name = self._available_artifact_name(
                    case_dir,
                    f"{artifact_prefix}-conflict-report.json",
                )
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

            case.update(
                {
                    "status": status,
                    "applied_commit": row.get("applied_commit"),
                    last_archive_field: archive_number,
                    "updated_at": self.now_iso(),
                }
            )
            case.setdefault("artifacts", {}).update(
                {"original_patch": "original.patch"}
                if (case_dir / "original.patch").is_file()
                else {}
            )
            archive_data = case.setdefault(archive_collection, {}).setdefault(
                archive_key, {}
            )
            archive_data.update(
                {
                    "operation": operation,
                    "status": status,
                    "applied_commit": row.get("applied_commit"),
                    "updated_at": self.now_iso(),
                }
            )
            archive_data.setdefault("artifacts", {}).update(artifacts)
            self.write_json(case_dir / "case.json", case)

        if cleanup:
            self.cleanup_work_dir(attempt_dir)
        return {
            "case_id": case_dir.name,
            "scope": archive_scope,
            "number": archive_number,
            "artifacts": artifacts,
        }

    def list_case_attempts(self, task_id: str, row_key: str) -> list[dict[str, Any]]:
        task_dir = self._task_dir(task_id)
        matches = [
            path for path in (task_dir / "cases").iterdir()
            if path.is_dir() and (path.name == row_key or path.name.startswith(f"{int(row_key):03d}-"))
        ] if (task_dir / "cases").is_dir() else []
        items: list[dict[str, Any]] = []
        for case_dir in matches:
            case = self._read_json(case_dir / "case.json")
            for collection, is_run in (("runs", True), ("interactions", False)):
                for archive_key, data in (case.get(collection) or {}).items():
                    if not isinstance(data, dict) or not str(archive_key).isdigit():
                        continue
                    artifacts = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
                    log_name = (
                        artifacts.get("engine_log")
                        or artifacts.get("apply_log")
                        or artifacts.get("backport_log")
                    )
                    items.append(
                        {
                            "execution": int(archive_key) if is_run else 0,
                            "attempt_number": 1 if is_run else int(archive_key),
                            "attempt_dir": str(case_dir),
                            "updated_at": str(
                                data.get("updated_at") or case.get("updated_at") or ""
                            ),
                            "report_path": "",
                            "stdout_path": "",
                            "stderr_path": str(case_dir / log_name) if log_name else "",
                            "rows": [
                                {
                                    "status": data.get("status"),
                                    "operation": data.get("operation"),
                                }
                            ],
                            "patches": [
                                {
                                    "kind": kind,
                                    "source": "",
                                    "archive": str(case_dir / name),
                                }
                                for kind, name in artifacts.items()
                                if name.endswith(".patch")
                            ],
                            "conflict_report": self._read_json(
                                case_dir / artifacts["conflict_report"]
                            )
                            if artifacts.get("conflict_report")
                            else None,
                        }
                    )
        return sorted(
            items,
            key=lambda item: (
                str(item["updated_at"]),
                int(item["execution"]),
                int(item["attempt_number"]),
            ),
            reverse=True,
        )

    def read_artifact(self, task_id: str, case_id: str, artifact: str) -> tuple[Path, bytes]:
        task_dir = self._task_dir(task_id)
        case_dir = (task_dir / "cases" / self.safe_slug(case_id)).resolve()
        if case_dir.parent != (task_dir / "cases").resolve() or case_dir.is_symlink():
            raise ValueError("Invalid case path.")
        case = self._read_json(case_dir / "case.json")
        registered = set((case.get("artifacts") or {}).values())
        for collection in ("runs", "interactions"):
            for archive in (case.get(collection) or {}).values():
                if isinstance(archive, dict):
                    registered.update((archive.get("artifacts") or {}).values())
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

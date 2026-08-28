from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from witty_service.api.backport_schemas import TargetConfigLayoutOpts
from witty_service.api.services import ServiceContainer
from witty_service.application.backport_commit_import import (
    serialize_commit_entries,
    validate_commit_entries,
)
from witty_service.application.backport_conflict_reporter_manager import (
    ConflictReporterManager,
)
from witty_service.application.backport_cvekit_client import (
    BackportCvekitClient,
    BackportRuntimeConfig,
)
from witty_service.application.backport_git_client import BackportGitClient
from witty_service.domain.errors import DomainError

logger = logging.getLogger(__name__)

DEFAULT_COMMIT_MESSAGE_TEMPLATE = """{{subject}}

commit {{commit_id}} {{source}}

{{body}}

{{trailers}}"""

BACKPORT_REPOSITORY_CACHE_ENV = "BACKPORT_REPOSITORY_CACHE_DIR"
BACKPORT_REPOSITORY_CACHE_DIR = "~/polymind-backport-repositories"
DEFAULT_PATCH_DATASET_DIR = "~/patched_output"
PREREQUISITE_REVIEW_STALE = "BACKPORT_PREREQUISITE_REVIEW_STALE"


def _default_config() -> dict[str, Any]:
    return {
        "project_url": "",
        "backport_model_id": "",
        "project_dir": "",
        "source_branch": "",
        "target_path": "",
        "target_release": "",
        "patch_dataset_dir": DEFAULT_PATCH_DATASET_DIR,
        "signer_name": "",
        "signer_email": "",
        "commit_message_template": DEFAULT_COMMIT_MESSAGE_TEMPLATE,
        "commit_message_source": "upstream",
        "linux_repo_path": "~/Image/linux",
        "commit_sort": "describe",
        "current_excel_path": "",
        "current_report_path": "",
        "current_filtered_report_path": "",
        "target_config_layout": "none",
        "target_config_layout_opts": {"default_level": "L1-RECOMMEND"},
        "source_repo_input": "",
        "target_repo_input": "",
        "source_repo_state": None,
        "target_repo_state": None,
        "enable_conflict_summary": False,
        "enable_prerequisite_scan": False,
        "cvekit_options": {},
    }


def _normalize_cvekit_options(config: dict[str, Any]) -> dict[str, Any]:
    raw_options = config.get("cvekit_options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
    legacy_enabled = config.get("enable_conflict_summary")
    if legacy_enabled is True:
        options["enable_conflict_summary"] = True
    elif "enable_conflict_summary" not in options and isinstance(legacy_enabled, bool):
        options["enable_conflict_summary"] = legacy_enabled
    config["cvekit_options"] = options
    return config


class BackportService:
    def __init__(
        self,
        services: ServiceContainer,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        pause_checker: Callable[[], bool] | None = None,
    ) -> None:
        self._config_path = (
            services.workspace_store.base_dir / "config" / "backport.json"
        )
        self._repository = services.repository
        self._git_client = BackportGitClient()
        self._cvekit_client = BackportCvekitClient(
            runs_root=services.workspace_store.base_dir / "backport-runs",
            progress_callback=self._handle_cvekit_progress,
        )
        self._conflict_reporter_manager = ConflictReporterManager()
        self._progress_callback = progress_callback
        self._pause_checker = pause_checker
        self._last_progress: dict[str, Any] = {}
        self._progress_before_repository_wait: dict[str, Any] = {}

    def _handle_cvekit_progress(self, event: dict[str, Any]) -> None:
        if self._progress_callback is None:
            return
        event_name = str(event.get("event") or "")
        if not event_name.startswith("repository_lock_"):
            return

        if event_name == "repository_lock_waiting":
            if self._last_progress.get("phase") != "waiting_for_repository":
                self._progress_before_repository_wait = dict(self._last_progress)
            wait_seconds = int(event.get("wait_seconds") or 0)
            timeout_seconds = int(event.get("timeout_seconds") or 0)
            progress = {
                **self._last_progress,
                "phase": "waiting_for_repository",
                "phase_state": "running",
                "message": (
                    "目标仓库正被另一项任务使用，"
                    f"已等待 {wait_seconds} 秒，最长等待 {timeout_seconds} 秒。"
                ),
            }
        elif event_name == "repository_lock_timeout":
            progress = {
                **self._last_progress,
                "phase": "waiting_for_repository",
                "phase_state": "failed",
                "message": "等待目标仓库可用超时，可稍后重试。",
            }
        elif event_name == "repository_lock_acquired":
            if not self._progress_before_repository_wait:
                # An uncontended lock acquisition is an implementation detail,
                # not a user-visible state transition.
                return
            progress = {
                **self._progress_before_repository_wait,
                **self._last_progress,
                "phase": (
                    self._progress_before_repository_wait.get("phase")
                    or self._last_progress.get("phase")
                    or "initializing"
                ),
                "phase_state": "running",
                "message": (
                    f"已获得目标仓库锁，等待 {int(event.get('wait_seconds') or 0)} 秒，"
                    "继续当前任务。"
                ),
            }
            self._progress_before_repository_wait = {}
        elif event_name in {
            "repository_lock_releasing",
            "repository_lock_released",
        }:
            return
        else:
            return

        owner = event.get("owner")
        if not isinstance(owner, dict):
            owner = {}
        progress.update(
            {
                "lock_event": event_name,
                "lock_wait_seconds": int(event.get("wait_seconds") or 0),
                "lock_timeout_seconds": int(event.get("timeout_seconds") or 0),
                "lock_owner_task_id": str(owner.get("task_id") or ""),
                "lock_owner_operation": str(owner.get("operation") or ""),
            }
        )
        self._last_progress = progress
        self._progress_callback(progress)

    def get_config(self) -> dict[str, Any]:
        if not self._config_path.exists():
            logger.info(
                "Backport config not found, using defaults: path=%s", self._config_path
            )
            return _default_config()

        try:
            loaded = json.loads(self._config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(
                code="BACKPORT_CONFIG_LOAD_FAILED",
                message="Backport config is invalid.",
                details={"path": str(self._config_path), "error": str(exc)},
            ) from exc

        config = _default_config()
        for key in config:
            value = loaded.get(key, "")
            if key in ("target_config_layout", "target_config_layout_opts"):
                continue  # handled by _normalize_layout_fields below
            default_value = config[key]
            if isinstance(default_value, bool):
                config[key] = value if isinstance(value, bool) else False
            elif isinstance(default_value, dict):
                config[key] = value if isinstance(value, dict) else {}
            elif default_value is None:
                config[key] = value if isinstance(value, dict) else None
            else:
                config[key] = value if isinstance(value, str) else ""
        self._normalize_layout_fields(config, loaded)
        config["commit_message_source"] = self._normalize_commit_message_source(
            config.get("commit_message_source", "")
        )
        _normalize_cvekit_options(config)
        logger.info(
            "Backport config loaded: path=%s target_path=%s target_release=%s current_report=%s",
            self._config_path,
            config["target_path"],
            config["target_release"],
            config["current_report_path"],
        )
        return config

    def update_config(self, payload: dict[str, Any]) -> None:
        config = _default_config()
        for key in config:
            if key in ("target_config_layout", "target_config_layout_opts"):
                continue
            value = payload.get(key, "")
            default_value = config[key]
            if isinstance(default_value, bool):
                config[key] = value if isinstance(value, bool) else False
            elif isinstance(default_value, dict):
                config[key] = value if isinstance(value, dict) else {}
            elif default_value is None:
                config[key] = value if isinstance(value, dict) else None
            else:
                config[key] = value.strip() if isinstance(value, str) else ""
        self._normalize_layout_fields(config, payload)
        config["commit_message_source"] = self._normalize_commit_message_source(
            config.get("commit_message_source", "")
        )
        _normalize_cvekit_options(config)

        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(
            "Backport config saved: path=%s target_path=%s target_release=%s excel=%s report=%s",
            self._config_path,
            config["target_path"],
            config["target_release"],
            config["current_excel_path"],
            config["current_report_path"],
        )

    @property
    def config_path(self) -> str:
        return str(self._config_path)

    def browse_path(self, raw_path: str | None = None) -> dict[str, Any]:
        root = Path.home().resolve()
        current_path = Path(raw_path or root).expanduser().resolve()
        try:
            current_path.relative_to(root)
        except ValueError as exc:
            raise DomainError(
                code="BACKPORT_BROWSE_PATH_FORBIDDEN",
                message="Backport browse path is outside the allowed root.",
                details={"path": str(current_path), "root": str(root)},
            ) from exc

        if current_path.is_file():
            current_path = current_path.parent

        if not current_path.is_dir():
            raise DomainError(
                code="BACKPORT_BROWSE_PATH_INVALID",
                message="Backport browse path is not a directory.",
                details={"path": str(current_path)},
            )

        entries = [
            {"name": item.name, "path": str(item), "is_dir": item.is_dir()}
            for item in sorted(
                current_path.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        ]
        parent_path = str(current_path.parent) if current_path != root else None
        return {
            "current_path": str(current_path),
            "parent_path": parent_path,
            "entries": entries,
        }

    def prepare_repository(
        self,
        *,
        role: str,
        raw_input: str,
        preferred_branch: str = "",
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        normalized_role = self._normalize_repository_role(role)
        repository_input = raw_input.strip()
        if not repository_input:
            raise ValueError("仓库地址不能为空")

        steps: list[dict[str, str]] = []

        def mark(title: str, status: str = "success", detail: str = "") -> None:
            steps.append({"title": title, "status": status, "detail": detail})
            if progress_callback is not None:
                progress_callback(
                    {"progress": min(95, len(steps) * 18), "steps": steps}
                )

        input_type = self._detect_repository_input_type(repository_input)
        mark(
            "已识别仓库地址",
            "success",
            "远程 Git 仓库" if input_type == "remote" else "服务器本地路径",
        )

        if input_type == "remote":
            cache_dir = self._repository_cache_dir()
            repos_dir = cache_dir / "repos"
            repos_dir.mkdir(parents=True, exist_ok=True)
            local_path = repos_dir / self._repository_cache_name(repository_input)
            if local_path.exists():
                mark("已找到本地缓存")
                self._run_repository_command(
                    ["git", "-C", str(local_path), "fetch", "--all", "--prune"]
                )
                mark("已同步远程分支")
            else:
                mark("开始克隆远程仓库", "running", str(local_path))
                self._run_repository_command(
                    ["git", "clone", repository_input, str(local_path)]
                )
                mark("仓库已克隆")
            source_url = repository_input
        else:
            local_path = Path(repository_input).expanduser().resolve()
            source_url = (
                BackportGitClient.remote_url(local_path) if local_path.exists() else ""
            )
            mark("已解析本地路径", "success", str(local_path))

        BackportGitClient.ensure_git_repo(local_path)
        mark("已确认 Git 仓库")

        repo_info = self._build_repository_info(
            role=normalized_role,
            repository_input=repository_input,
            input_type=input_type,
            local_path=local_path,
            source_url=source_url,
            selected_branch=preferred_branch.strip(),
        )
        self._remember_repository(repo_info)
        mark("已读取分支与仓库状态")
        return repo_info | {"steps": steps}

    def refresh_repository(
        self,
        *,
        role: str,
        local_path: str,
        source_url: str = "",
        selected_branch: str = "",
    ) -> dict[str, Any]:
        normalized_role = self._normalize_repository_role(role)
        repo_path = Path(local_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo_path)
        resolved_source_url = (
            BackportGitClient.remote_url(repo_path) or source_url.strip()
        )
        repo_info = self._build_repository_info(
            role=normalized_role,
            repository_input=resolved_source_url or str(repo_path),
            input_type="remote" if resolved_source_url else "local",
            local_path=repo_path,
            source_url=resolved_source_url,
            selected_branch=selected_branch.strip(),
        )
        self._remember_repository(repo_info)
        return repo_info

    def list_recent_repositories(self) -> dict[str, Any]:
        index_path = self._repository_index_path()
        if not index_path.exists():
            return {"repositories": []}
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"repositories": []}
        repositories = payload.get("repositories")
        if not isinstance(repositories, list):
            return {"repositories": []}
        return {"repositories": repositories[:20]}

    def get_runtime_status(
        self, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        config = (
            self._extract_config({"config": payload})
            if isinstance(payload, dict)
            else self.get_config()
        )
        errors: list[str] = []
        status: dict[str, Any] = {
            "ok": False,
            "model_configured": False,
            "model_name": "",
            "model_provider": "",
            "api_key_available": False,
            "cvekit_available": False,
            "cvekit_path": "",
            "errors": errors,
        }

        try:
            model = self._resolve_backport_model(config)
            provider = self._resolve_cvekit_llm_provider(
                model.provider, model.compatibility
            )
            status["model_configured"] = True
            status["model_name"] = model.name
            status["model_provider"] = provider
            status["api_key_available"] = (
                bool(model.api_key.strip()) or provider == "local"
            )
            if not model.name.strip():
                errors.append("Backport 运行模型缺少模型 ID。")
            if not status["api_key_available"]:
                errors.append("Backport 运行模型缺少 API Key。")
            if (
                provider
                not in {"openai", "deepseek", "siliconflow", "minimax", "local"}
                and not (model.api_base_url or "").strip()
            ):
                errors.append("Backport 运行模型缺少 API Base URL。")
        except RuntimeError as error:
            errors.append(str(error))

        try:
            cvekit_path = self._cvekit_client.resolve_cvekit_path()
            status["cvekit_available"] = True
            status["cvekit_path"] = str(cvekit_path)
        except (FileNotFoundError, RuntimeError) as error:
            errors.append(str(error))

        status["ok"] = (
            status["model_configured"]
            and status["api_key_available"]
            and status["cvekit_available"]
            and not errors
        )
        return status

    def run_action(
        self, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        normalized_action = action.strip()
        handlers = self._build_handlers()
        handler = handlers.get(normalized_action)
        if handler is None:
            raise DomainError(
                code="BACKPORT_ACTION_NOT_SUPPORTED",
                message="Backport action is not supported.",
                details={"action": normalized_action or action},
            )
        normalized_payload = payload if isinstance(payload, dict) else {}
        started_at = time.monotonic()
        logger.info(
            "Backport action started: action=%s payload_keys=%s",
            normalized_action,
            sorted(normalized_payload.keys()),
        )
        conflict_reporter_started = False
        runtime_configured = False
        archive_run_id = self._get_string(
            normalized_payload, "_archive_run_id", "archive_run_id"
        )
        if archive_run_id:
            self._cvekit_client.set_archive_run_id(archive_run_id)
        lock_target = ""
        try:
            action_config: dict[str, Any] | None = None
            needs_config = (
                normalized_action in self._cvekit_runtime_actions()
                or normalized_action in {"run_all", "execute_selected", "try_resolve"}
            )
            if needs_config:
                action_config = self._extract_config(normalized_payload)
            # 目标仓库跨进程锁:所有执行 cvekit 的 action 加锁,覆盖执行生命周期
            # (同一 target_path 的并发操作互斥;无 target_path 的 action 跳过)
            if needs_config:
                try:
                    lock_target = self._resolve_target_path(
                        normalized_payload,
                        action_config or {},
                        operation=normalized_action,
                    )
                except DomainError:
                    lock_target = ""
                if lock_target:
                    self._cvekit_client.set_lock_target(lock_target)
            if normalized_action in self._cvekit_runtime_actions():
                self._cvekit_client.set_runtime_config(
                    self._resolve_cvekit_runtime_config(action_config or {})
                )
                runtime_configured = True
            if normalized_action in {"run_all", "execute_selected", "try_resolve"}:
                cvekit_options = (
                    action_config.get("cvekit_options")
                    if action_config
                    and isinstance(action_config.get("cvekit_options"), dict)
                    else {}
                )
                conflict_reporter_url = self._conflict_reporter_manager.start(
                    enabled=bool(cvekit_options.get("enable_conflict_summary")),
                )
                self._cvekit_client.set_conflict_reporter_url(conflict_reporter_url)
                conflict_reporter_started = True
            # 目标仓库锁覆盖整个 run 操作(多次 cvekit 调用 + git 操作),
            # 防止并发 run_all 在阶段间交错;锁可重入,_run_cvekit 嵌套进入不重复加锁
            with self._cvekit_client.repository_lock():
                parsed_result = handler(normalized_payload)
            self._persist_runtime_state(
                normalized_action, normalized_payload, parsed_result
            )
            elapsed = time.monotonic() - started_at
            status = parsed_result.get("status")
            logger.info(
                "Backport action completed: action=%s status=%s elapsed=%.2fs",
                normalized_action,
                status,
                elapsed,
            )
            return self._build_response(parsed_result)
        except Exception:
            logger.exception(
                "Backport action failed before response: action=%s elapsed=%.2fs",
                normalized_action,
                time.monotonic() - started_at,
            )
            raise
        finally:
            if conflict_reporter_started:
                self._cvekit_client.set_conflict_reporter_url("")
                self._conflict_reporter_manager.stop()
            if runtime_configured:
                self._cvekit_client.set_runtime_config(None)
            if archive_run_id:
                self._cvekit_client.set_archive_run_id(None)
            if lock_target:
                self._cvekit_client.set_lock_target(None)

    def _build_handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "run_all": self._run_all,
            "generate_report": self._run_generate_report,
            "load_report": self._run_load_report,
            "continue_report": self._run_continue_report,
            "recheck_conflict": self._run_recheck_conflict,
            "load_git_log": self._run_load_git_log,
            "load_git_show": self._run_load_git_show,
            "load_patch_preview": self._run_load_patch_preview,
            "preview_commit_message": self._run_preview_commit_message,
            "execute_selected": self._run_execute_selected,
            "apply_row": self._run_apply_row,
            "try_resolve": self._run_try_resolve,
            "check_manual_patch": self._run_check_manual_patch,
            "apply_manual_patch": self._run_apply_manual_patch,
            "prerequisite_commits": self._run_prerequisite_commits,
        }

    @staticmethod
    def _cvekit_runtime_actions() -> set[str]:
        return {
            "run_all",
            "generate_report",
            "continue_report",
            "recheck_conflict",
            "preview_commit_message",
            "execute_selected",
            "apply_row",
            "try_resolve",
            "check_manual_patch",
            "apply_manual_patch",
        }

    # ── 业务方法 ──────────────────────────────────────────────

    def _run_all(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        current_report_path = (
            self._get_string(
                payload,
                "base_report_path",
                "baseReportPath",
                "working_report_path",
                "workingReportPath",
            )
            or config["current_report_path"]
        )

        if current_report_path and Path(current_report_path).expanduser().exists():
            loaded = self._cvekit_client.load_report(current_report_path)
            current_report_path = self._resolve_report_path(loaded)
            current_commits = self._resolve_result_commits(loaded)
            self._emit_run_all_progress(
                phase="initializing",
                phase_state="completed",
                message="已加载当前 Backport 工作表，准备从现有状态继续。",
                current_report_path=current_report_path,
                commits=current_commits,
                updated_commits=[],
            )
        else:
            self._require_string(payload, "run_all", "excel_path", "excelPath")
            self._emit_run_all_progress(
                phase="initializing",
                phase_state="running",
                message="当前没有可继续的 report，正在从 Excel 初始化工作表。",
                current_report_path="",
                commits=[],
                updated_commits=[],
            )
            generated = self._run_generate_report(payload)
            if generated.get("status") == "failed":
                return {**generated, "operation": "run_all"}
            current_report_path = self._resolve_report_path(generated)
            current_commits = self._resolve_result_commits(generated)
            self._emit_run_all_progress(
                phase="initializing",
                phase_state="completed",
                message="工作表已初始化，后续将按 commit 顺序逐条处理。",
                current_report_path=current_report_path,
                commits=current_commits,
                updated_commits=current_commits,
            )

        if not current_report_path:
            return {
                "operation": "run_all",
                "status": "failed",
                "stage": "failed",
                "summary": "一键运行未找到可用 report 路径",
                "diagnostics": {"error_text": "Missing report path for run_all."},
            }

        target_baseline_sha = self._cvekit_client.pin_target_title_index_baseline(
            base_report_path=current_report_path,
            target_path=self._resolve_target_path(payload, config, operation="run_all"),
        )
        self._emit_run_all_progress(
            phase="initializing",
            phase_state="completed",
            message=f"已固定本轮目标标题索引基线: {target_baseline_sha[:12]}。",
            current_report_path=current_report_path,
            commits=current_commits,
            updated_commits=[],
        )

        failed_count = 0
        processed_count = 0
        index = 0
        step_count = 0
        max_steps = int(payload.get("max_steps") or max(len(current_commits) * 4, 20))

        if self._is_run_all_pause_requested():
            return self._build_run_all_paused_result(
                current_report_path=current_report_path,
                current_commits=current_commits,
                failed_count=failed_count,
                processed_count=processed_count,
            )

        # 一键运行必须按顺序推进：前序 commit 合入后会改变目标仓状态，
        # 后序 commit 不能依赖一次性整表预检查的结果。
        while index < len(current_commits) and step_count < max_steps:
            step_count += 1
            loaded = self._cvekit_client.load_report(current_report_path)
            current_commits = self._resolve_result_commits(loaded)
            if index >= len(current_commits):
                break

            if self._is_run_all_pause_requested():
                return self._build_run_all_paused_result(
                    current_report_path=current_report_path,
                    current_commits=current_commits,
                    failed_count=failed_count,
                    processed_count=processed_count,
                )

            row = current_commits[index]
            row_status = str(row.get("status") or "").strip().lower()
            has_apply_candidate = any(
                str(row.get(key) or "").strip()
                for key in (
                    "backported_patch_path",
                    "patch_path",
                    "original_patch_path",
                )
            )
            was_unchecked = row_status == "pending" or row.get("has_conflict") is None
            base_progress = {
                "current_index": index + 1,
                "total": len(current_commits),
                "current_commit": str(
                    row.get("commit") or row.get("input_commit") or ""
                ),
                "current_title": self._describe_commit_row(row),
                "current_row_id": str(
                    row.get("row_id")
                    or row.get("commit")
                    or row.get("input_commit")
                    or ""
                ),
                "failed_count": failed_count,
                "processed_count": processed_count,
            }

            if self._is_terminal_nonblocking_row(row):
                if row_status in {"failed", "error"}:
                    failed_count += 1
                processed_count += 1
                self._emit_run_all_progress(
                    phase="skipped",
                    phase_state="completed",
                    message=f"跳过第 {index + 1} 条：已完成、无需移植或已标记失败。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=[row],
                    failed_count=failed_count,
                    processed_count=processed_count,
                    **{
                        key: value
                        for key, value in base_progress.items()
                        if key not in {"failed_count", "processed_count"}
                    },
                )
                index += 1
                continue

            # 如果已经有 patch 文件，先尝试直接应用；失败后再回退到单行检查/解冲突。
            # 这样避免 pending 行每次都启动 cvekit 做完整检查，绕开批量 cache 无法跨进程复用的问题。
            needs_check = was_unchecked and not has_apply_candidate
            if needs_check:
                self._emit_run_all_progress(
                    phase="checking",
                    phase_state="running",
                    message=f"正在检查第 {index + 1} 条 commit。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=[row],
                    **base_progress,
                )
                checked = self._run_check_row(
                    {
                        "config": config,
                        "base_report_path": current_report_path,
                        "working_report_path": current_report_path,
                        "row": row,
                    }
                )
                updated_rows = self._resolve_result_commits(checked)
                if checked.get("status") == "failed":
                    failed_count += 1
                    processed_count += 1
                    self._emit_run_all_progress(
                        phase="failed",
                        phase_state="failed",
                        message=f"第 {index + 1} 条 commit 检查失败，已标记失败并继续后续 commit。",
                        current_report_path=current_report_path,
                        commits=current_commits,
                        updated_commits=updated_rows or [row],
                        failed_count=failed_count,
                        processed_count=processed_count,
                        **{
                            key: value
                            for key, value in base_progress.items()
                            if key not in {"failed_count", "processed_count"}
                        },
                    )
                    index += 1
                    continue
                self._emit_run_all_progress(
                    phase="checking",
                    phase_state="completed",
                    message=f"第 {index + 1} 条 commit 检查完成。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=updated_rows,
                    **base_progress,
                )
                continue

            if row.get("has_conflict") is True:
                self._emit_run_all_progress(
                    phase="resolving",
                    phase_state="running",
                    message=f"第 {index + 1} 条存在冲突，正在尝试自动处理。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=[row],
                    **base_progress,
                )
                resolved = self._run_try_resolve(
                    {
                        "config": config,
                        "base_report_path": current_report_path,
                        "working_report_path": current_report_path,
                        "row": row,
                    }
                )
                resolved_rows = self._resolve_result_commits(resolved)
                unresolved = (
                    resolved.get("status") == "failed"
                    or self._find_blocking_conflict(resolved_rows) is not None
                )
                if unresolved:
                    failed_count += 1
                    failed_rows = [
                        {
                            **(item if isinstance(item, dict) else row),
                            "status": "failed",
                            "has_conflict": True,
                            "error": resolved.get("summary") or "自动解冲突失败",
                            "conflict_check_error": (
                                resolved.get("summary")
                                or (resolved.get("diagnostics") or {}).get("error_text")
                                or row.get("conflict_check_error")
                            ),
                        }
                        for item in (resolved_rows or [row])
                    ]
                    merged = self._cvekit_client.merge_rows_into_report(
                        current_report_path, failed_rows
                    )
                    updated_rows = self._resolve_result_commits(merged)
                    processed_count += 1
                    self._emit_run_all_progress(
                        phase="failed",
                        phase_state="failed",
                        message=f"第 {index + 1} 条自动解冲突失败，已标记失败并继续后续 commit。",
                        current_report_path=current_report_path,
                        commits=current_commits,
                        updated_commits=updated_rows,
                        failed_count=failed_count,
                        processed_count=processed_count,
                        **{
                            key: value
                            for key, value in base_progress.items()
                            if key not in {"failed_count", "processed_count"}
                        },
                    )
                    index += 1
                    continue

                self._emit_run_all_progress(
                    phase="resolving",
                    phase_state="completed",
                    message=f"第 {index + 1} 条自动处理完成，准备应用。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=resolved_rows or [row],
                    **base_progress,
                )
                loaded = self._cvekit_client.load_report(current_report_path)
                current_commits = self._resolve_result_commits(loaded)
                row = current_commits[index]
                row_status = str(row.get("status") or "").strip().lower()
                if self._is_terminal_nonblocking_row(row):
                    if row_status in {"failed", "error"}:
                        failed_count += 1
                    processed_count += 1
                    self._emit_run_all_progress(
                        phase="skipped",
                        phase_state="completed",
                        message=f"第 {index + 1} 条自动处理后已完成或无需再次应用。",
                        current_report_path=current_report_path,
                        commits=current_commits,
                        updated_commits=[row],
                        failed_count=failed_count,
                        processed_count=processed_count,
                        **{
                            key: value
                            for key, value in base_progress.items()
                            if key not in {"failed_count", "processed_count"}
                        },
                    )
                    index += 1
                    continue

            self._emit_run_all_progress(
                phase="applying",
                phase_state="running",
                message=f"正在应用第 {index + 1} 条 commit。",
                current_report_path=current_report_path,
                commits=current_commits,
                updated_commits=[row],
                **base_progress,
            )
            applied = self._run_apply_row(
                {
                    "config": config,
                    "base_report_path": current_report_path,
                    "working_report_path": current_report_path,
                    "row": row,
                }
            )
            applied_rows = self._resolve_result_commits(applied)
            if applied.get("status") == "failed" and was_unchecked:
                self._emit_run_all_progress(
                    phase="checking",
                    phase_state="running",
                    message=f"第 {index + 1} 条直接应用失败，正在回退到冲突检查。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=applied_rows or [row],
                    **base_progress,
                )
                checked = self._run_check_row(
                    {
                        "config": config,
                        "base_report_path": current_report_path,
                        "working_report_path": current_report_path,
                        "row": applied_rows[0] if applied_rows else row,
                    }
                )
                updated_rows = self._resolve_result_commits(checked)
                if checked.get("status") == "failed":
                    failed_count += 1
                    processed_count += 1
                    self._emit_run_all_progress(
                        phase="failed",
                        phase_state="failed",
                        message=f"第 {index + 1} 条 commit 检查失败，已标记失败并继续后续 commit。",
                        current_report_path=current_report_path,
                        commits=current_commits,
                        updated_commits=updated_rows or [row],
                        failed_count=failed_count,
                        processed_count=processed_count,
                        **{
                            key: value
                            for key, value in base_progress.items()
                            if key not in {"failed_count", "processed_count"}
                        },
                    )
                    index += 1
                    continue
                self._emit_run_all_progress(
                    phase="checking",
                    phase_state="completed",
                    message=f"第 {index + 1} 条 commit 检查完成。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=updated_rows,
                    **base_progress,
                )
                continue

            if applied.get("status") == "failed":
                failed_count += 1
            processed_count += 1
            self._emit_run_all_progress(
                phase="failed" if applied.get("status") == "failed" else "applying",
                phase_state="failed"
                if applied.get("status") == "failed"
                else "completed",
                message=applied.get("summary")
                or f"第 {index + 1} 条 commit 应用完成。",
                current_report_path=current_report_path,
                commits=current_commits,
                updated_commits=applied_rows,
                failed_count=failed_count,
                processed_count=processed_count,
                **{
                    key: value
                    for key, value in base_progress.items()
                    if key not in {"failed_count", "processed_count"}
                },
            )
            index += 1

        if step_count >= max_steps and index < len(current_commits):
            final_report = self._cvekit_client.load_report(current_report_path)
            final_commits = self._resolve_result_commits(final_report)
            archive_artifacts = (
                self._cvekit_client.archive_artifacts_for_report(current_report_path)
                if hasattr(self._cvekit_client, "archive_artifacts_for_report")
                else {}
            )
            return {
                "operation": "run_all",
                "status": "failed",
                "stage": "failed",
                "summary": f"一键运行超过最大步数 {max_steps}，仍未完成。",
                "artifacts": {
                    "base_report_path": current_report_path,
                    **archive_artifacts,
                },
                "report": {
                    "report_path": current_report_path,
                    "commit_count": len(final_commits),
                    "commits": final_commits,
                },
                "diagnostics": {"error_text": "run_all reached max_steps."},
            }

        final_report = self._cvekit_client.load_report(current_report_path)
        final_commits = self._resolve_result_commits(final_report)
        archive_artifacts = (
            self._cvekit_client.archive_artifacts_for_report(current_report_path)
            if hasattr(self._cvekit_client, "archive_artifacts_for_report")
            else {}
        )
        self._emit_run_all_progress(
            phase="completed",
            phase_state="completed",
            message="一键运行完成。",
            current_report_path=current_report_path,
            commits=final_commits,
            updated_commits=[],
            current_index=len(final_commits),
            total=len(final_commits),
            failed_count=failed_count,
            processed_count=processed_count,
        )
        return {
            "operation": "run_all",
            "status": "success",
            "stage": "completed",
            "summary": f"一键运行完成，共处理 {processed_count} 条 commit，失败 {failed_count} 条。",
            "artifacts": {"base_report_path": current_report_path, **archive_artifacts},
            "report": {
                "report_path": current_report_path,
                "commit_count": len(final_commits),
                "commits": final_commits,
            },
            "report_artifacts": {},
            "conflict_report_summary": {
                "status": "not_implemented",
                "message": "冲突总报告内容预留，后续从工具结果接入。",
            },
        }

    def _run_generate_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        commit_entries = self._validated_payload_commit_entries(payload, config)
        excel_path = self._get_string(payload, "excel_path", "excelPath")
        if bool(excel_path) == (commit_entries is not None):
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="generate_report 必须且只能提供 excel_path 或 commit_entries。",
            )
        logger.info(
            "Backport generate_report inputs: source=%s project_dir=%s source_branch=%s target_path=%s target_release=%s patch_dataset_dir=%s",
            excel_path or "commit_entries",
            config["project_dir"],
            config["source_branch"],
            config["target_path"],
            config["target_release"],
            config["patch_dataset_dir"],
        )
        try:
            prerequisite_commits = payload.get("prerequisite_commits")
            if isinstance(prerequisite_commits, list):
                self._validate_prerequisite_review(
                    payload.get("prerequisite_review"),
                    excel_path=excel_path,
                    commit_entries=commit_entries,
                    config=config,
                )
            return self._cvekit_client.generate_report(
                excel_path=excel_path or None,
                project_url=config["project_url"],
                project_dir=config["project_dir"],
                source_branch=config["source_branch"],
                target_path=config["target_path"],
                target_release=config["target_release"],
                patch_dataset_dir=config["patch_dataset_dir"],
                signer_name=config["signer_name"],
                signer_email=config["signer_email"],
                commit_message_template=config["commit_message_template"],
                commit_message_source=config["commit_message_source"],
                linux_repo_path=config["linux_repo_path"],
                commit_sort=config["commit_sort"],
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
                prerequisite_commits=prerequisite_commits,
                commit_entries=commit_entries,
            )
        except DomainError as error:
            logger.warning("generate_report prerequisite review rejected: %s", error)
            return {
                "operation": "generate_report",
                "status": "failed",
                "summary": error.message,
                "diagnostics": {"code": error.code, **error.details},
            }
        except (RuntimeError, FileNotFoundError, NotADirectoryError, ValueError) as error:
            logger.exception("generate_report failed")
            return {
                "operation": "generate_report",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_prerequisite_commits(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        commit_entries = self._validated_payload_commit_entries(payload, config)
        excel_path = self._get_string(payload, "excel_path", "excelPath")
        if bool(excel_path) == (commit_entries is not None):
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="prerequisite_commits 必须且只能提供 excel_path 或 commit_entries。",
            )
        if not str(config["project_dir"] or "").strip():
            raise DomainError(
                code="BACKPORT_REPOSITORY_NOT_CONFIGURED",
                message="前置提交查找需要配置 source 仓库路径（项目目录）。",
                details={"action": "prerequisite_commits", "keys": ["config.project_dir"]},
            )
        if not str(config["target_path"] or "").strip():
            raise DomainError(
                code="BACKPORT_REPOSITORY_NOT_CONFIGURED",
                message="前置提交查找需要配置 target 仓库路径（目标目录）。",
                details={"action": "prerequisite_commits", "keys": ["config.target_path"]},
            )
        try:
            original_commits = (
                commit_entries
                if commit_entries is not None
                else self._cvekit_client.extract_config_commits(excel_path, config)
            )
            shas = [
                str(row["commit"])
                for row in original_commits
                if isinstance(row, dict) and row.get("commit")
            ]
            target_ref = BackportGitClient.resolve_ref(
                config["target_path"],
                config["target_release"] or "HEAD",
            )
            manifest = self._cvekit_client.prerequisite_commits(
                source_repo=config["project_dir"],
                target_repo=config["target_path"],
                target_ref=target_ref,
                prereq_commits=shas,
            )
            input_digest = str(manifest.get("input_digest") or "").strip()
            manifest_target_ref = str(manifest.get("target_ref") or "").strip()
            if not input_digest:
                raise RuntimeError("Patchflow 前置提交结果缺少 input_digest。")
            if manifest_target_ref != target_ref:
                raise RuntimeError("Patchflow 前置提交结果的 target_ref 与请求基线不一致。")
            manifest["review"] = self._build_prerequisite_review(
                excel_path=excel_path,
                commit_entries=commit_entries,
                config=config,
                input_digest=input_digest,
                target_ref=manifest_target_ref,
            )
            candidates = manifest.get("candidates") or []
            return {
                "operation": "prerequisite_commits",
                "status": "success",
                "summary": f"扫描完成，建议前置提交 {len(candidates)} 条",
                "manifest": manifest,
                "original_commits": original_commits,
                "report": {
                    "commits": original_commits,
                    "commit_count": len(original_commits),
                },
            }
        except (RuntimeError, FileNotFoundError, NotADirectoryError, ValueError) as error:
            logger.exception("prerequisite_commits failed")
            return {
                "operation": "prerequisite_commits",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_load_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = (
            self._get_string(payload, "base_report_path", "baseReportPath")
            or config["current_report_path"]
        )
        if not base_report_path:
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="Missing required argument for load_report.",
                details={
                    "action": "load_report",
                    "keys": ["base_report_path", "baseReportPath"],
                },
            )
        try:
            return self._cvekit_client.load_report(base_report_path=base_report_path)
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("load_report failed")
            return {
                "operation": "load_report",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_continue_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = (
            self._get_string(payload, "base_report_path", "baseReportPath")
            or config["current_report_path"]
        )
        if not base_report_path:
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="Missing required argument for continue_report.",
                details={
                    "action": "continue_report",
                    "keys": ["base_report_path", "baseReportPath"],
                },
            )
        try:
            return self._cvekit_client.continue_report(
                base_report_path=base_report_path,
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("continue_report failed")
            return {
                "operation": "continue_report",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_recheck_conflict(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = (
            self._get_string(payload, "base_report_path", "baseReportPath")
            or config["current_report_path"]
        )
        working_report_path = self._get_string(
            payload, "working_report_path", "workingReportPath"
        )
        row = payload.get("row")
        if not base_report_path:
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="Missing required argument for recheck_conflict.",
                details={
                    "action": "recheck_conflict",
                    "keys": ["base_report_path", "baseReportPath"],
                },
            )
        if not isinstance(row, dict):
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="Missing required row for recheck_conflict.",
                details={"action": "recheck_conflict", "keys": ["row"]},
            )
        try:
            return self._cvekit_client.recheck_conflict(
                base_report_path=base_report_path,
                working_report_path=working_report_path,
                row=row,
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("recheck_conflict failed")
            return {
                "operation": "recheck_conflict",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_load_git_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        try:
            target_path = self._resolve_target_path(
                payload, config, operation="load_git_log"
            )
            entries = self._git_client.load_git_log(target_path, limit=100)
            return {
                "operation": "load_git_log",
                "status": "success",
                "summary": f"loaded {len(entries)} commits",
                "git": {"entries": entries},
            }
        except (DomainError, FileNotFoundError, NotADirectoryError) as error:
            logger.exception("load_git_log failed")
            return {
                "operation": "load_git_log",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_load_git_show(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        try:
            target_path = self._resolve_target_path(
                payload, config, operation="load_git_show"
            )
            revision = self._require_string(payload, "load_git_show", "revision")
            show_content = self._git_client.load_git_show(target_path, revision)
            return {
                "operation": "load_git_show",
                "status": "success",
                "git": {
                    "revision": revision,
                    "show_content": show_content,
                },
            }
        except (
            DomainError,
            RuntimeError,
            FileNotFoundError,
            NotADirectoryError,
        ) as error:
            logger.exception("load_git_show failed")
            return {
                "operation": "load_git_show",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_execute_selected(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = self._require_string(
            payload, "execute_selected", "base_report_path", "baseReportPath"
        )
        working_report_path = self._get_string(
            payload,
            "working_report_path",
            "workingReportPath",
            "current_filtered_report_path",
            "currentFilteredReportPath",
        )
        selected_commits = payload.get("selected_commits")
        if not isinstance(selected_commits, list) or not selected_commits:
            raise DomainError(
                code="BACKPORT_SELECTED_COMMITS_INVALID",
                message="selected_commits must be a non-empty array.",
                details={"action": "execute_selected"},
            )
        try:
            return self._cvekit_client.execute_selected(
                base_report_path=base_report_path,
                selected_commits=selected_commits,
                target_path=self._resolve_target_path(
                    payload, config, operation="execute_selected"
                ),
                patch_dataset_dir=config["patch_dataset_dir"],
                signer_name=config["signer_name"],
                signer_email=config["signer_email"],
                commit_message_template=config["commit_message_template"],
                commit_message_source=config["commit_message_source"],
                linux_repo_path=config["linux_repo_path"],
                working_report_path=working_report_path,
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
                cvekit_options=config["cvekit_options"],
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("execute_selected failed")
            return {
                "operation": "execute_selected",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_apply_row(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = self._require_string(
            payload, "apply_row", "base_report_path", "baseReportPath"
        )
        working_report_path = self._get_string(
            payload,
            "working_report_path",
            "workingReportPath",
            "current_filtered_report_path",
            "currentFilteredReportPath",
        )
        row = payload.get("row")
        if not isinstance(row, dict) or not row:
            raise DomainError(
                code="BACKPORT_ROW_INVALID",
                message="row must be a non-empty object.",
                details={"action": "apply_row"},
            )
        try:
            return self._cvekit_client.apply_row(
                base_report_path=base_report_path,
                row=row,
                commit_message_template=config["commit_message_template"],
                commit_message_source=config["commit_message_source"],
                signer_name=config["signer_name"],
                signer_email=config["signer_email"],
                linux_repo_path=config["linux_repo_path"],
                working_report_path=working_report_path,
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("apply_row failed")
            failed_row = dict(row)
            failed_row["status"] = "failed"
            failed_row["error"] = str(error)
            return {
                "operation": "apply_row",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
                "report": {"commit_count": 1, "commits": [failed_row]},
            }

    def _run_check_row(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = self._require_string(
            payload, "check_row", "base_report_path", "baseReportPath"
        )
        working_report_path = self._get_string(
            payload,
            "working_report_path",
            "workingReportPath",
            "current_filtered_report_path",
            "currentFilteredReportPath",
        )
        row = payload.get("row")
        if not isinstance(row, dict) or not row:
            raise DomainError(
                code="BACKPORT_ROW_INVALID",
                message="row must be a non-empty object.",
                details={"action": "check_row"},
            )
        try:
            return self._cvekit_client.check_row(
                base_report_path=base_report_path,
                working_report_path=working_report_path,
                row=row,
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("check_row failed")
            failed_row = dict(row)
            failed_row["status"] = "failed"
            failed_row["error"] = str(error)
            failed_row["conflict_check_error"] = str(error)
            try:
                merged = self._cvekit_client.merge_rows_into_report(
                    base_report_path, [failed_row]
                )
                return {
                    **merged,
                    "operation": "check_row",
                    "status": "failed",
                    "summary": str(error),
                    "diagnostics": {"error_text": str(error)},
                }
            except (RuntimeError, FileNotFoundError, ValueError):
                logger.exception("failed to persist check_row failure")
                return {
                    "operation": "check_row",
                    "status": "failed",
                    "summary": str(error),
                    "diagnostics": {"error_text": str(error)},
                    "report": {"commit_count": 1, "commits": [failed_row]},
                }

    def _run_try_resolve(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = self._require_string(
            payload, "try_resolve", "base_report_path", "baseReportPath"
        )
        working_report_path = self._get_string(
            payload,
            "working_report_path",
            "workingReportPath",
            "current_filtered_report_path",
            "currentFilteredReportPath",
        )
        row = payload.get("row")
        if not isinstance(row, dict) or not row:
            raise DomainError(
                code="BACKPORT_ROW_INVALID",
                message="row must be a non-empty object.",
                details={"action": "try_resolve"},
            )
        try:
            return self._cvekit_client.try_resolve(
                base_report_path=base_report_path,
                row=row,
                target_path=self._resolve_target_path(
                    payload, config, operation="try_resolve"
                ),
                patch_dataset_dir=config["patch_dataset_dir"],
                signer_name=config["signer_name"],
                signer_email=config["signer_email"],
                commit_message_template=config["commit_message_template"],
                commit_message_source=config["commit_message_source"],
                linux_repo_path=config["linux_repo_path"],
                working_report_path=working_report_path,
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
                cvekit_options=config["cvekit_options"],
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("try_resolve failed")
            failed_row = dict(row)
            failed_row["status"] = "failed"
            failed_row["error"] = str(error)
            return {
                "operation": "try_resolve",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
                "report": {"commit_count": 1, "commits": [failed_row]},
            }

    def _run_preview_commit_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = self._require_string(
            payload,
            "preview_commit_message",
            "base_report_path",
            "baseReportPath",
        )
        working_report_path = self._get_string(
            payload,
            "working_report_path",
            "workingReportPath",
            "current_filtered_report_path",
            "currentFilteredReportPath",
        )
        row = payload.get("row")
        if not isinstance(row, dict) or not row:
            raise DomainError(
                code="BACKPORT_ROW_INVALID",
                message="row must be a non-empty object.",
                details={"action": "preview_commit_message"},
            )
        template_override = self._get_string(
            payload, "commit_message_template", "commitMessageTemplate"
        )
        try:
            return self._cvekit_client.preview_commit_message(
                base_report_path=base_report_path,
                row=row,
                commit_message_template=template_override
                or config["commit_message_template"],
                commit_message_source=config["commit_message_source"],
                linux_repo_path=config["linux_repo_path"],
                working_report_path=working_report_path,
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("preview_commit_message failed")
            return {
                "operation": "preview_commit_message",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_check_manual_patch(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        try:
            target_path = self._resolve_target_path(
                payload, config, operation="check_manual_patch"
            )
            patch_text = self._require_string(
                payload, "check_manual_patch", "patch_text", "patchText"
            )
            result = self._git_client.check_manual_patch(target_path, patch_text)
            ok = result["returncode"] == "0"
            return {
                "operation": "check_manual_patch",
                "status": "success" if ok else "failed",
                "summary": "手动 Patch 可以干净应用" if ok else "手动 Patch 检查失败",
                "manual_patch": result,
                "diagnostics": {"error_text": result["stderr"]} if not ok else {},
            }
        except (
            DomainError,
            RuntimeError,
            FileNotFoundError,
            NotADirectoryError,
            ValueError,
        ) as error:
            logger.exception("check_manual_patch failed")
            return {
                "operation": "check_manual_patch",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_apply_manual_patch(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        try:
            target_path = self._resolve_target_path(
                payload, config, operation="apply_manual_patch"
            )
            patch_text = self._require_string(
                payload, "apply_manual_patch", "patch_text", "patchText"
            )
            result = self._git_client.apply_manual_patch(target_path, patch_text)
            ok = result["returncode"] == "0"
            return {
                "operation": "apply_manual_patch",
                "status": "success" if ok else "failed",
                "summary": "手动 Patch 已应用到目标仓" if ok else "手动 Patch 应用失败",
                "manual_patch": result,
                "diagnostics": {"error_text": result["stderr"]} if not ok else {},
            }
        except (
            DomainError,
            RuntimeError,
            FileNotFoundError,
            NotADirectoryError,
            ValueError,
        ) as error:
            logger.exception("apply_manual_patch failed")
            return {
                "operation": "apply_manual_patch",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_load_patch_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        base_report_path = self._require_string(
            payload, "load_patch_preview", "base_report_path", "baseReportPath"
        )
        working_report_path = self._get_string(
            payload,
            "working_report_path",
            "workingReportPath",
            "current_filtered_report_path",
            "currentFilteredReportPath",
        )
        patch_kind = self._require_string(
            payload, "load_patch_preview", "patch_kind", "patchKind"
        )
        row = payload.get("row")
        if not isinstance(row, dict) or not row:
            raise DomainError(
                code="BACKPORT_ROW_INVALID",
                message="row must be a non-empty object.",
                details={"action": "load_patch_preview"},
            )
        try:
            return self._cvekit_client.load_patch_preview(
                base_report_path=base_report_path,
                working_report_path=working_report_path,
                row=row,
                patch_kind=patch_kind,
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("load_patch_preview failed")
            return {
                "operation": "load_patch_preview",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    # ── 辅助方法 ──────────────────────────────────────────────

    @staticmethod
    def _normalize_layout_fields(
        config: dict[str, Any], source: dict[str, Any]
    ) -> None:
        """校验并合并 source 中的 layout 字段到 config。

        layout/opts 成对处理：
        - layout 显式非法 → none + 默认 opts
        - layout 显式 anolis → 保留；opts 合法合并，缺失/非法用默认
        - layout 缺失 + opts 非法 → 重置两者
        """
        layout_in_source = "target_config_layout" in source
        opts_in_source = "target_config_layout_opts" in source

        # 预校验 opts（如果存在）
        opts_valid = False
        validated_opts: dict[str, Any] | None = None
        if opts_in_source:
            raw_opts = source["target_config_layout_opts"]
            if isinstance(raw_opts, dict):
                try:
                    validated_opts = TargetConfigLayoutOpts(**raw_opts).model_dump()
                    opts_valid = True
                except Exception:
                    pass  # nosec B110 - 无效配置按未提供处理

        # Step 1: 处理 layout
        if layout_in_source:
            raw_layout = source["target_config_layout"]
            if isinstance(raw_layout, str) and raw_layout.strip() in {"none", "anolis"}:
                config["target_config_layout"] = raw_layout.strip()
            else:
                # 显式非法 → 重置
                config["target_config_layout"] = "none"
                config["target_config_layout_opts"] = {"default_level": "L1-RECOMMEND"}
                return
        elif opts_in_source and not opts_valid:
            # layout 缺失 + opts 非法 → 重置两者
            config["target_config_layout"] = "none"
            config["target_config_layout_opts"] = {"default_level": "L1-RECOMMEND"}
            return

        effective_layout = config["target_config_layout"]

        # Step 2: 处理 opts (runtime payload 完全替换 persisted opts)
        if opts_in_source and opts_valid:
            config["target_config_layout_opts"] = validated_opts
        elif opts_in_source and not opts_valid:
            # 非法 opts → 默认（但保留 layout）
            config["target_config_layout_opts"] = {"default_level": "L1-RECOMMEND"}
        elif effective_layout != "none" and layout_in_source:
            # layout 显式设为非 none，但没传 opts → 默认
            config["target_config_layout_opts"] = {"default_level": "L1-RECOMMEND"}

    def _extract_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_config = payload.get("config")
        normalized = self.get_config()
        if not isinstance(raw_config, dict):
            return normalized

        for key in _default_config():
            if key in ("target_config_layout", "target_config_layout_opts"):
                continue
            value = raw_config.get(key)
            if isinstance(normalized.get(key), bool):
                if isinstance(value, bool):
                    normalized[key] = value
                continue
            if isinstance(normalized.get(key), dict):
                if isinstance(value, dict):
                    normalized[key] = dict(value)
                continue
            if isinstance(value, str):
                stripped = value.strip()
                if stripped or key.startswith("current_"):
                    normalized[key] = stripped
        if not str(normalized.get("patch_dataset_dir") or "").strip():
            normalized["patch_dataset_dir"] = DEFAULT_PATCH_DATASET_DIR
        _normalize_cvekit_options(normalized)
        self._normalize_layout_fields(normalized, raw_config)
        return normalized

    def _resolve_cvekit_runtime_config(
        self, config: dict[str, Any]
    ) -> BackportRuntimeConfig:
        model = self._resolve_backport_model(config)
        provider = self._resolve_cvekit_llm_provider(
            model.provider, model.compatibility
        )
        base_url = (model.api_base_url or "").strip()
        model_name = model.name.strip()
        api_key = model.api_key.strip()

        if not model_name:
            raise RuntimeError(
                "Backport 运行模型缺少模型 ID，请在 Backport 配置区选择有效模型。"
            )
        if not api_key and provider != "local":
            raise RuntimeError(
                "Backport 运行模型缺少 API Key，请在模型设置页补全密钥。"
            )
        if (
            provider not in {"openai", "deepseek", "siliconflow", "minimax", "local"}
            and not base_url
        ):
            raise RuntimeError(
                "Backport 运行模型缺少 API Base URL，无法作为 cvekit 自定义模型启动。"
            )

        return BackportRuntimeConfig(
            llm_provider=provider,
            api_key=api_key,
            llm_base_url=base_url,
            llm_model_name=model_name,
            backport_engine="opencode",
            format_mode="changed",
        )

    def _resolve_backport_model(self, config: dict[str, Any]) -> Any:
        model_id = str(config.get("backport_model_id") or "").strip()
        if model_id:
            model = self._repository.get_model(model_id)
            if model is None:
                raise RuntimeError("Backport 运行模型不存在，请重新选择模型。")
            if not model.enabled:
                raise RuntimeError("Backport 运行模型已禁用，请重新选择模型。")
            return model

        models = [model for model in self._repository.list_models() if model.enabled]
        default_models = [model for model in models if model.is_default]
        if default_models:
            return default_models[-1]
        if len(models) == 1:
            return models[0]
        raise RuntimeError("Backport 未选择运行模型，请在 Backport 配置区选择模型。")

    @staticmethod
    def _resolve_cvekit_llm_provider(provider: str, compatibility: str | None) -> str:
        normalized = provider.strip().lower()
        if normalized == "custom":
            if (compatibility or "").strip().lower() != "openai":
                raise RuntimeError("Backport 只支持 OpenAI-compatible 的自定义模型。")
            return "custom"
        supported = {
            "openai",
            "deepseek",
            "siliconflow",
            "minimax",
            "local",
            "moonshotai",
            "zhipuai",
            "xai",
            "alibaba",
        }
        if normalized not in supported:
            raise RuntimeError(
                f"Backport 暂不支持 provider={provider!r} 的模型，请选择 OpenAI-compatible 模型。"
            )
        return normalized

    def _persist_runtime_state(
        self,
        action: str,
        payload: dict[str, Any],
        parsed_result: dict[str, Any],
    ) -> None:
        report = parsed_result.get("report")
        if not isinstance(report, dict):
            report = {}
        artifacts = parsed_result.get("artifacts")
        if not isinstance(artifacts, dict):
            artifacts = {}

        config = self._extract_config(payload)
        if action in {"generate_report", "run_all"}:
            config["current_excel_path"] = self._get_string(
                payload, "excel_path", "excelPath"
            )
            if action == "generate_report":
                config["current_filtered_report_path"] = ""

        report_path = (
            self._get_string(artifacts, "base_report_path")
            or self._get_string(artifacts, "report_path")
            or self._get_string(report, "report_path")
        )
        if (
            action
            in {
                "generate_report",
                "continue_report",
                "recheck_conflict",
                "try_resolve",
                "run_all",
            }
            and report_path
        ):
            config["current_report_path"] = report_path

        filtered_report_path = self._get_string(artifacts, "filtered_report_path")
        if action in {"execute_selected", "run_all"} and filtered_report_path:
            config["current_filtered_report_path"] = filtered_report_path
        self.update_config(config)

    def _emit_run_all_progress(
        self,
        *,
        phase: str,
        phase_state: str,
        message: str,
        current_report_path: str,
        commits: list[dict[str, Any]],
        updated_commits: list[dict[str, Any]],
        current_index: int = 0,
        total: int | None = None,
        current_commit: str = "",
        current_title: str = "",
        current_row_id: str = "",
        failed_count: int = 0,
        processed_count: int = 0,
    ) -> None:
        if self._progress_callback is None:
            return
        safe_updated = self._cvekit_client.sanitize_commit_list(updated_commits)
        progress = {
            "phase": phase,
            "phase_state": phase_state,
            "message": message,
            "current_report_path": current_report_path,
            "current_index": current_index,
            "total": total if total is not None else len(commits),
            "current_commit": current_commit,
            "current_title": current_title,
            "current_row_id": current_row_id,
            "processed_count": processed_count,
            "failed_count": failed_count,
            "updated_commits": safe_updated,
            "conflict_report_summary": {
                "status": "not_implemented",
                "message": "冲突总报告内容预留，后续从工具结果接入。",
            },
        }
        self._last_progress = progress
        try:
            self._progress_callback(progress)
        except Exception:
            logger.exception("Backport run_all progress callback failed")

    def _is_run_all_pause_requested(self) -> bool:
        checker = getattr(self, "_pause_checker", None)
        if checker is None:
            return False
        try:
            return bool(checker())
        except Exception:
            logger.exception("Backport run_all pause checker failed")
            return False

    def _build_run_all_paused_result(
        self,
        *,
        current_report_path: str,
        current_commits: list[dict[str, Any]],
        failed_count: int,
        processed_count: int,
    ) -> dict[str, Any]:
        self._emit_run_all_progress(
            phase="paused",
            phase_state="completed",
            message="已暂停，report 已保存，可继续。",
            current_report_path=current_report_path,
            commits=current_commits,
            updated_commits=[],
            current_index=min(processed_count + 1, len(current_commits))
            if current_commits
            else 0,
            total=len(current_commits),
            failed_count=failed_count,
            processed_count=processed_count,
        )
        archive_artifacts = (
            self._cvekit_client.archive_artifacts_for_report(current_report_path)
            if hasattr(self._cvekit_client, "archive_artifacts_for_report")
            else {}
        )
        return {
            "operation": "run_all",
            "status": "success",
            "stage": "paused",
            "summary": "一键运行已暂停，当前 report 已保存，可继续。",
            "artifacts": {"base_report_path": current_report_path, **archive_artifacts},
            "report": {
                "report_path": current_report_path,
                "commit_count": len(current_commits),
                "commits": current_commits,
            },
            "report_artifacts": {},
            "conflict_report_summary": None,
        }

    @staticmethod
    def _is_skipped_commit(item: dict[str, Any]) -> bool:
        status = str(item.get("status") or "").strip().lower()
        merged = str(item.get("merged_in_target") or "").strip().lower()
        return (
            status == "skipped"
            or merged == "skipped"
            or item.get("is_merge_commit") is True
        )

    @classmethod
    def _is_terminal_nonblocking_row(cls, row: dict[str, Any]) -> bool:
        """行是否已完成且非阻塞冲突：已合入/空 patch/已应用/等价存在/失败。"""
        status = str(row.get("status") or "").strip().lower()
        return (
            cls._is_skipped_commit(row)
            or row.get("merged_in_target") is True
            or row.get("empty_patch") is True
            or row.get("equivalent_exists") is True
            or bool(str(row.get("applied_commit") or "").strip())
            or status in {"failed", "error"}
        )

    @classmethod
    def _find_blocking_conflict(
        cls, commits: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in commits
                if isinstance(item, dict)
                and item.get("has_conflict") is True
                and str(item.get("status") or "").strip().lower()
                not in {"failed", "error"}
                and not cls._is_skipped_commit(item)
            ),
            None,
        )

    @staticmethod
    def _has_pending_commit(commits: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(item, dict)
            and str(item.get("status") or "").strip().lower() == "pending"
            for item in commits
        )

    @staticmethod
    def _resolve_report_path(parsed_result: dict[str, Any]) -> str:
        artifacts = parsed_result.get("artifacts")
        if not isinstance(artifacts, dict):
            artifacts = {}
        report = parsed_result.get("report")
        if not isinstance(report, dict):
            report = {}
        return (
            BackportService._get_string(artifacts, "base_report_path")
            or BackportService._get_string(artifacts, "report_path")
            or BackportService._get_string(report, "report_path")
        )

    @staticmethod
    def _resolve_result_commits(parsed_result: dict[str, Any]) -> list[dict[str, Any]]:
        report = parsed_result.get("report")
        if not isinstance(report, dict):
            return []
        commits = report.get("commits")
        return (
            [item for item in commits if isinstance(item, dict)]
            if isinstance(commits, list)
            else []
        )

    def validate_commit_entries_for_payload(
        self, payload: dict[str, Any]
    ) -> list[dict[str, str]]:
        """Validate a confirmation request before it creates an async Task."""
        config = self._extract_config(payload)
        entries = self._validated_payload_commit_entries(payload, config)
        if entries is None:
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="commit_entries 不能为空。",
                details={"action": "generate_report", "keys": ["commit_entries"]},
            )
        return entries

    @staticmethod
    def _commit_import_error(errors: list[dict[str, Any]]) -> DomainError:
        return DomainError(
            code="BACKPORT_COMMIT_IMPORT_INVALID",
            message="提交清单校验失败。",
            details={"errors": errors},
        )

    def _validated_payload_commit_entries(
        self, payload: dict[str, Any], config: dict[str, Any]
    ) -> list[dict[str, str]] | None:
        if "commit_entries" not in payload:
            return None
        raw_entries = payload.get("commit_entries")
        if not isinstance(raw_entries, list):
            raise self._commit_import_error(
                [{"field": "commit_entries", "message": "commit_entries 必须是数组。"}]
            )
        checked = validate_commit_entries(raw_entries)
        if checked.errors:
            raise self._commit_import_error(checked.errors)
        source_path = str(config.get("project_dir") or "").strip()
        if not source_path:
            raise DomainError(
                code="BACKPORT_REPOSITORY_NOT_CONFIGURED",
                message="提交清单确认需要配置 source 仓库路径（项目目录）。",
                details={"action": "commit_entries", "keys": ["config.project_dir"]},
            )
        missing: list[dict[str, Any]] = []
        for index, entry in enumerate(checked.entries, start=1):
            try:
                BackportGitClient.resolve_commit(source_path, entry["commit"])
            except (FileNotFoundError, NotADirectoryError, RuntimeError) as error:
                missing.append(
                    {
                        "row": index,
                        "field": "commit",
                        "message": str(error),
                    }
                )
        if missing:
            raise self._commit_import_error(missing)
        return checked.entries

    @staticmethod
    def _describe_commit_row(row: dict[str, Any]) -> str:
        for key in ("row_id", "commit", "input_commit", "title", "commit_title"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "<unknown>"

    @staticmethod
    def _build_prerequisite_review(
        *,
        excel_path: str,
        commit_entries: list[dict[str, str]] | None = None,
        config: dict[str, Any],
        input_digest: str,
        target_ref: str,
    ) -> dict[str, str]:
        source_fingerprint: dict[str, str]
        if commit_entries is not None:
            source_fingerprint = {
                "commit_entries_sha256": hashlib.sha256(
                    serialize_commit_entries(commit_entries).encode("utf-8")
                ).hexdigest()
            }
        else:
            excel = Path(excel_path).expanduser().resolve()
            excel_hasher = hashlib.sha256()
            with excel.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    excel_hasher.update(chunk)
            source_fingerprint = {"excel_sha256": excel_hasher.hexdigest()}
        snapshot = {
            **source_fingerprint,
            "input_digest": input_digest.strip(),
            "source_repo": str(
                Path(str(config.get("project_dir") or "")).expanduser().resolve()
            ),
            "source_branch": str(config.get("source_branch") or "").strip(),
            "target_repo": str(
                Path(str(config.get("target_path") or "")).expanduser().resolve()
            ),
            "target_release": str(config.get("target_release") or "").strip(),
            "target_ref": target_ref.strip(),
        }
        encoded = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {**snapshot, "review_version": hashlib.sha256(encoded).hexdigest()}

    def _validate_prerequisite_review(
        self,
        review: Any,
        *,
        excel_path: str,
        commit_entries: list[dict[str, str]] | None = None,
        config: dict[str, Any],
    ) -> None:
        if not isinstance(review, dict):
            raise DomainError(
                code=PREREQUISITE_REVIEW_STALE,
                message="前置提交审阅缺少版本信息，请重新扫描。",
            )
        required = {
            "commit_entries_sha256" if commit_entries is not None else "excel_sha256",
            "input_digest",
            "source_repo",
            "source_branch",
            "target_repo",
            "target_release",
            "target_ref",
            "review_version",
        }
        if any(not isinstance(review.get(key), str) for key in required) or any(
            not str(review.get(key) or "").strip()
            for key in (
                "commit_entries_sha256" if commit_entries is not None else "excel_sha256",
                "input_digest",
                "source_repo",
                "target_repo",
                "target_ref",
                "review_version",
            )
        ):
            raise DomainError(
                code=PREREQUISITE_REVIEW_STALE,
                message="前置提交审阅版本不完整，请重新扫描。",
            )

        target_ref = BackportGitClient.resolve_ref(
            str(config.get("target_path") or ""),
            str(config.get("target_release") or "HEAD"),
        )
        current = self._build_prerequisite_review(
            excel_path=excel_path,
            commit_entries=commit_entries,
            config=config,
            input_digest=str(review["input_digest"]),
            target_ref=target_ref,
        )
        if any(str(review.get(key)) != value for key, value in current.items()):
            raise DomainError(
                code=PREREQUISITE_REVIEW_STALE,
                message="提交输入或仓库基线已变化，请重新扫描并审阅前置提交。",
                details={
                    "review_version": str(review.get("review_version") or ""),
                    "current_version": current["review_version"],
                },
            )

    def _require_string(self, payload: dict[str, Any], operation: str, *keys: str) -> str:
        value = self._get_string(payload, *keys)
        if value:
            return value
        raise DomainError(
            code="BACKPORT_ARGUMENT_REQUIRED",
            message=f"Missing required argument for {operation}.",
            details={"action": operation, "keys": list(keys)},
        )

    @staticmethod
    def _get_string(payload: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _normalize_commit_message_source(value: str) -> str:
        return value if value in {"auto", "openEuler", "upstream"} else "upstream"

    @staticmethod
    def _normalize_repository_role(role: str) -> str:
        normalized = role.strip().lower()
        if normalized not in {"source", "target"}:
            raise ValueError("仓库角色必须是 source 或 target")
        return normalized

    @staticmethod
    def _detect_repository_input_type(repository_input: str) -> str:
        stripped = repository_input.strip()
        parsed = urlparse(stripped)
        if parsed.scheme in {"http", "https", "ssh", "git"}:
            return "remote"
        if re.match(r"^[^@\s]+@[^:\s]+:.+", stripped):
            return "remote"
        return "local"

    @staticmethod
    def _repository_cache_name(repository_input: str) -> str:
        clean_input = repository_input.strip().removesuffix("/")
        parsed = urlparse(clean_input)
        if parsed.path:
            raw_name = Path(parsed.path).name
        elif ":" in clean_input:
            raw_name = Path(clean_input.split(":", 1)[1]).name
        else:
            raw_name = Path(clean_input).name
        name = raw_name.removesuffix(".git") or "repository"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "repository"
        digest = hashlib.sha1(
            clean_input.encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:12]
        return f"{safe_name}-{digest}"

    @staticmethod
    def _run_repository_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "命令执行失败"
            raise RuntimeError(message)
        return result

    @staticmethod
    def _git_dir(repo: Path) -> Path | None:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            return None
        git_dir = Path(result.stdout.strip())
        return git_dir if git_dir.is_absolute() else (repo / git_dir).resolve()

    def _repository_cache_dir(self) -> Path:
        raw_dir = os.environ.get(
            BACKPORT_REPOSITORY_CACHE_ENV, BACKPORT_REPOSITORY_CACHE_DIR
        )
        return Path(raw_dir).expanduser().resolve()

    def _repository_index_path(self) -> Path:
        return self._repository_cache_dir() / "repositories.json"

    def _build_repository_info(
        self,
        *,
        role: str,
        repository_input: str,
        input_type: str,
        local_path: Path,
        source_url: str,
        selected_branch: str,
    ) -> dict[str, Any]:
        head = BackportGitClient.head(local_path)
        current_branch = BackportGitClient.current_branch(local_path)
        default_branch = BackportGitClient.default_branch(local_path)
        local_branches, remote_branches = BackportGitClient.branches(local_path)
        available_branches = set(local_branches) | set(remote_branches)
        resolved_branch = selected_branch or current_branch or default_branch
        status_args = (
            ["status", "--porcelain=v1", "-uall"]
            if role == "target"
            else ["status", "--porcelain=v1", "-uno"]
        )
        status_result = BackportGitClient._run_git(local_path, status_args)
        status_clean = (
            status_result.returncode == 0 and status_result.stdout.strip() == ""
        )
        git_dir = self._git_dir(local_path)
        in_progress = False
        if git_dir is not None:
            in_progress = any(
                (git_dir / item).exists()
                for item in (
                    "MERGE_HEAD",
                    "CHERRY_PICK_HEAD",
                    "REBASE_HEAD",
                    "rebase-merge",
                    "rebase-apply",
                )
            )
        display_source = source_url or repository_input
        display_name = self._repository_display_name(display_source, local_path)
        writable = os.access(local_path, os.W_OK)
        warnings: list[str] = []
        if selected_branch and selected_branch not in available_branches:
            fallback_branch = (
                current_branch
                or default_branch
                or (local_branches[0] if local_branches else "")
            )
            if fallback_branch:
                resolved_branch = fallback_branch
            warnings.append(
                f"分支 {selected_branch} 不存在，已回退到 {resolved_branch or '默认 HEAD'}。"
            )
        if role == "target" and not status_clean:
            warnings.append("目标仓库存在未提交修改，建议清理后再执行回移植。")
        if role == "target" and in_progress:
            warnings.append("目标仓库存在未完成的 merge/rebase/cherry-pick。")
        if role == "target" and not writable:
            warnings.append("目标仓库目录不可写。")
        return {
            "role": role,
            "input": repository_input,
            "input_type": input_type,
            "display_name": display_name,
            "source_url": source_url,
            "local_path": str(local_path),
            "default_branch": default_branch,
            "selected_branch": resolved_branch,
            "current_branch": current_branch,
            "head": head,
            "short_head": head[:12],
            "local_branches": local_branches,
            "remote_branches": remote_branches,
            "status_clean": status_clean,
            "operation_in_progress": in_progress,
            "writable": writable,
            "can_read": True,
            "can_write": role != "target"
            or (writable and status_clean and not in_progress),
            "warnings": warnings,
            "cache_dir": str(self._repository_cache_dir()),
            "updated_at": time.time(),
        }

    @staticmethod
    def _repository_display_name(source: str, local_path: Path) -> str:
        if source:
            stripped = source.strip().removesuffix("/")
            parsed = urlparse(stripped)
            candidate = parsed.path if parsed.path else stripped.split(":", 1)[-1]
            name = Path(candidate).name.removesuffix(".git")
            if name:
                parent = Path(candidate).parent.name
                return f"{parent}/{name}" if parent and parent != "." else name
        return local_path.name

    def _remember_repository(self, repo_info: dict[str, Any]) -> None:
        index_path = self._repository_index_path()
        index_path.parent.mkdir(parents=True, exist_ok=True)
        repositories: list[dict[str, Any]] = []
        if index_path.exists():
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
                raw_repositories = payload.get("repositories")
                if isinstance(raw_repositories, list):
                    repositories = [
                        item for item in raw_repositories if isinstance(item, dict)
                    ]
            except json.JSONDecodeError:
                repositories = []
        repo_key = repo_info.get("source_url") or repo_info.get("local_path")
        next_repositories = [
            item
            for item in repositories
            if (item.get("source_url") or item.get("local_path")) != repo_key
            or item.get("role") != repo_info.get("role")
        ]
        next_repositories.insert(0, repo_info)
        index_path.write_text(
            json.dumps(
                {"repositories": next_repositories[:30]}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )

    def _resolve_target_path(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
        *,
        operation: str,
    ) -> str:
        target_path = (
            self._get_string(payload, "target_path", "targetPath")
            or config["target_path"]
        )
        if target_path:
            return target_path
        raise DomainError(
            code="BACKPORT_TARGET_PATH_REQUIRED",
            message="target_path is required.",
            details={"action": operation},
        )

    # ── 响应格式 ──────────────────────────────────────────────

    def _build_response(self, parsed_result: dict[str, Any]) -> dict[str, Any]:
        sanitized_result = self._sanitize_parsed_result(parsed_result)
        operation = sanitized_result.get("operation", "unknown")
        is_error = sanitized_result.get("status") == "failed"
        if operation == "load_patch_preview":
            return {
                "agentId": "backport-direct",
                "agentName": "backport-direct-api",
                "sessionId": f"direct-{int(time.time() * 1000)}",
                "assistantText": "",
                "parsedResult": sanitized_result,
                "toolSnapshots": [],
            }

        combined_output = json.dumps(sanitized_result, ensure_ascii=False, indent=2)
        return {
            "agentId": "backport-direct",
            "agentName": "backport-direct-api",
            "sessionId": f"direct-{int(time.time() * 1000)}",
            "assistantText": self._build_assistant_text(sanitized_result),
            "parsedResult": sanitized_result,
            "toolSnapshots": [
                {
                    "tool_name": f"backport.{operation}",
                    "arguments_text": combined_output,
                    "response_text": combined_output,
                    "is_error": is_error,
                }
            ],
        }

    def _sanitize_parsed_result(self, parsed_result: dict[str, Any]) -> dict[str, Any]:
        sanitized = json.loads(json.dumps(parsed_result, ensure_ascii=False))
        report = sanitized.get("report")
        if not isinstance(report, dict):
            return sanitized
        commits = report.get("commits")
        if not isinstance(commits, list):
            return sanitized
        report["commits"] = self._cvekit_client.sanitize_commit_list(commits)
        sanitized["report"] = report
        return sanitized

    @staticmethod
    def _build_assistant_text(parsed_result: dict[str, Any] | None) -> str:
        if parsed_result is None:
            return ""
        return (
            "<backport_result>\n"
            + json.dumps(parsed_result, ensure_ascii=False, indent=2)
            + "\n</backport_result>"
        )

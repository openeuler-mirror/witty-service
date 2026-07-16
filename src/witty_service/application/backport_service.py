from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from witty_service.api.backport_schemas import TargetConfigLayoutOpts
from witty_service.api.services import ServiceContainer
from witty_service.application.backport_cvekit_client import BackportCvekitClient
from witty_service.application.backport_git_client import BackportGitClient
from witty_service.domain.errors import DomainError


logger = logging.getLogger(__name__)

DEFAULT_COMMIT_MESSAGE_TEMPLATE = """{{subject}}

commit {{commit_id}} {{source}}

{{body}}

{{trailers}}"""


def _default_config() -> dict[str, Any]:
    return {
        "project_url": "",
        "project_dir": "",
        "source_branch": "",
        "target_path": "",
        "target_release": "",
        "patch_dataset_dir": "",
        "signer_name": "",
        "signer_email": "",
        "commit_message_template": DEFAULT_COMMIT_MESSAGE_TEMPLATE,
        "commit_message_source": "auto",
        "linux_repo_path": "~/Image/linux",
        "commit_sort": "describe",
        "current_excel_path": "",
        "current_report_path": "",
        "current_filtered_report_path": "",
        "target_config_layout": "none",
        "target_config_layout_opts": {"default_level": "L1-RECOMMEND"},
    }


class BackportService:
    def __init__(
        self,
        services: ServiceContainer,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._config_path = services.workspace_store.base_dir / "config" / "backport.json"
        self._git_client = BackportGitClient()
        self._cvekit_client = BackportCvekitClient(
            runs_root=services.workspace_store.base_dir / "backport-runs",
        )
        self._progress_callback = progress_callback

    def get_config(self) -> dict[str, Any]:
        if not self._config_path.exists():
            logger.info("Backport config not found, using defaults: path=%s", self._config_path)
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
            config[key] = value if isinstance(value, str) else ""
        self._normalize_layout_fields(config, loaded)
        config["commit_message_source"] = self._normalize_commit_message_source(
            config.get("commit_message_source", "")
        )
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
            config[key] = value.strip() if isinstance(value, str) else ""
        self._normalize_layout_fields(config, payload)
        config["commit_message_source"] = self._normalize_commit_message_source(
            config.get("commit_message_source", "")
        )

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
            for item in sorted(current_path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        ]
        parent_path = str(current_path.parent) if current_path != root else None
        return {"current_path": str(current_path), "parent_path": parent_path, "entries": entries}

    def run_action(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
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
        try:
            parsed_result = handler(normalized_payload)
            self._persist_runtime_state(normalized_action, normalized_payload, parsed_result)
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
        }

    # ── 业务方法 ──────────────────────────────────────────────

    def _run_all(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        current_report_path = (
            self._get_string(payload, "base_report_path", "baseReportPath", "working_report_path", "workingReportPath")
            or config["current_report_path"]
        )

        if current_report_path and Path(current_report_path).expanduser().exists():
            loaded = self._cvekit_client.load_report(current_report_path)
            current_report_path = self._resolve_report_path(loaded)
            current_commits = self._resolve_result_commits(loaded)
            self._emit_run_all_progress(
                phase="initializing",
                message="已加载当前 Backport 工作表，准备从现有状态继续。",
                current_report_path=current_report_path,
                commits=current_commits,
                updated_commits=[],
            )
        else:
            self._require_string(payload, "run_all", "excel_path", "excelPath")
            self._emit_run_all_progress(
                phase="initializing",
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

        # 一键运行必须按顺序推进：前序 commit 合入后会改变目标仓状态，
        # 后序 commit 不能依赖一次性整表预检查的结果。
        while index < len(current_commits) and step_count < max_steps:
            step_count += 1
            loaded = self._cvekit_client.load_report(current_report_path)
            current_commits = self._resolve_result_commits(loaded)
            if index >= len(current_commits):
                break

            row = current_commits[index]
            row_status = str(row.get("status") or "").strip().lower()
            has_apply_candidate = any(
                str(row.get(key) or "").strip()
                for key in ("backported_patch_path", "patch_path", "original_patch_path")
            )
            was_unchecked = row_status == "pending" or row.get("has_conflict") is None
            base_progress = {
                "current_index": index + 1,
                "total": len(current_commits),
                "current_commit": str(row.get("commit") or row.get("input_commit") or ""),
                "current_title": self._describe_commit_row(row),
                "current_row_id": str(row.get("row_id") or row.get("commit") or row.get("input_commit") or ""),
                "failed_count": failed_count,
                "processed_count": processed_count,
            }

            if (
                self._is_skipped_commit(row)
                or row.get("merged_in_target") is True
                or row.get("empty_patch") is True
                or row.get("equivalent_exists") is True
                or str(row.get("applied_commit") or "").strip()
                or row_status in {"failed", "error"}
            ):
                if row_status in {"failed", "error"}:
                    failed_count += 1
                processed_count += 1
                self._emit_run_all_progress(
                    phase="skipped",
                    message=f"跳过第 {index + 1} 条：已完成、无需移植或已标记失败。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=[row],
                    failed_count=failed_count,
                    processed_count=processed_count,
                    **{key: value for key, value in base_progress.items() if key not in {"failed_count", "processed_count"}},
                )
                index += 1
                continue

            # 如果已经有 patch 文件，先尝试直接应用；失败后再回退到单行检查/解冲突。
            # 这样避免 pending 行每次都启动 cvekit 做完整检查，绕开批量 cache 无法跨进程复用的问题。
            needs_check = was_unchecked and not has_apply_candidate
            if needs_check:
                self._emit_run_all_progress(
                    phase="checking",
                    message=f"正在检查第 {index + 1} 条 commit。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=[row],
                    **base_progress,
                )
                checked = self._run_check_row(
                    {
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
                        message=f"第 {index + 1} 条 commit 检查失败，已标记失败并继续后续 commit。",
                        current_report_path=current_report_path,
                        commits=current_commits,
                        updated_commits=updated_rows or [row],
                        failed_count=failed_count,
                        processed_count=processed_count,
                        **{key: value for key, value in base_progress.items() if key not in {"failed_count", "processed_count"}},
                    )
                    index += 1
                    continue
                self._emit_run_all_progress(
                    phase="checking",
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
                unresolved = resolved.get("status") == "failed" or self._find_blocking_conflict(resolved_rows) is not None
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
                    merged = self._cvekit_client.merge_rows_into_report(current_report_path, failed_rows)
                    updated_rows = self._resolve_result_commits(merged)
                    processed_count += 1
                    self._emit_run_all_progress(
                        phase="failed",
                        message=f"第 {index + 1} 条自动解冲突失败，已标记失败并继续后续 commit。",
                        current_report_path=current_report_path,
                        commits=current_commits,
                        updated_commits=updated_rows,
                        failed_count=failed_count,
                        processed_count=processed_count,
                        **{key: value for key, value in base_progress.items() if key not in {"failed_count", "processed_count"}},
                    )
                    index += 1
                    continue

                self._emit_run_all_progress(
                    phase="resolving",
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
                if (
                    self._is_skipped_commit(row)
                    or row.get("merged_in_target") is True
                    or row.get("empty_patch") is True
                    or row.get("equivalent_exists") is True
                    or str(row.get("applied_commit") or "").strip()
                    or row_status in {"failed", "error"}
                ):
                    if row_status in {"failed", "error"}:
                        failed_count += 1
                    processed_count += 1
                    self._emit_run_all_progress(
                        phase="skipped",
                        message=f"第 {index + 1} 条自动处理后已完成或无需再次应用。",
                        current_report_path=current_report_path,
                        commits=current_commits,
                        updated_commits=[row],
                        failed_count=failed_count,
                        processed_count=processed_count,
                        **{key: value for key, value in base_progress.items() if key not in {"failed_count", "processed_count"}},
                    )
                    index += 1
                    continue

            self._emit_run_all_progress(
                phase="applying",
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
                    message=f"第 {index + 1} 条直接应用失败，正在回退到冲突检查。",
                    current_report_path=current_report_path,
                    commits=current_commits,
                    updated_commits=applied_rows or [row],
                    **base_progress,
                )
                checked = self._run_check_row(
                    {
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
                        message=f"第 {index + 1} 条 commit 检查失败，已标记失败并继续后续 commit。",
                        current_report_path=current_report_path,
                        commits=current_commits,
                        updated_commits=updated_rows or [row],
                        failed_count=failed_count,
                        processed_count=processed_count,
                        **{key: value for key, value in base_progress.items() if key not in {"failed_count", "processed_count"}},
                    )
                    index += 1
                    continue
                self._emit_run_all_progress(
                    phase="checking",
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
                message=applied.get("summary") or f"第 {index + 1} 条 commit 应用完成。",
                current_report_path=current_report_path,
                commits=current_commits,
                updated_commits=applied_rows,
                failed_count=failed_count,
                processed_count=processed_count,
                **{key: value for key, value in base_progress.items() if key not in {"failed_count", "processed_count"}},
            )
            index += 1

        if step_count >= max_steps and index < len(current_commits):
            final_report = self._cvekit_client.load_report(current_report_path)
            final_commits = self._resolve_result_commits(final_report)
            return {
                "operation": "run_all",
                "status": "failed",
                "stage": "failed",
                "summary": f"一键运行超过最大步数 {max_steps}，仍未完成。",
                "artifacts": {"base_report_path": current_report_path},
                "report": {
                    "report_path": current_report_path,
                    "commit_count": len(final_commits),
                    "commits": final_commits,
                },
                "diagnostics": {"error_text": "run_all reached max_steps."},
            }

        final_report = self._cvekit_client.load_report(current_report_path)
        final_commits = self._resolve_result_commits(final_report)
        self._emit_run_all_progress(
            phase="completed",
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
            "artifacts": {"base_report_path": current_report_path},
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
        excel_path = self._require_string(payload, "generate_report", "excel_path", "excelPath")
        logger.info(
            "Backport generate_report inputs: excel=%s project_dir=%s source_branch=%s target_path=%s target_release=%s patch_dataset_dir=%s",
            excel_path,
            config["project_dir"],
            config["source_branch"],
            config["target_path"],
            config["target_release"],
            config["patch_dataset_dir"],
        )
        try:
            return self._cvekit_client.generate_report(
                excel_path=excel_path,
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
            )
        except (RuntimeError, FileNotFoundError, NotADirectoryError, ValueError) as error:
            logger.exception("generate_report failed")
            return {
                "operation": "generate_report",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_load_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = self._get_string(payload, "base_report_path", "baseReportPath") or config["current_report_path"]
        if not base_report_path:
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="Missing required argument for load_report.",
                details={"action": "load_report", "keys": ["base_report_path", "baseReportPath"]},
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
        base_report_path = self._get_string(payload, "base_report_path", "baseReportPath") or config["current_report_path"]
        if not base_report_path:
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="Missing required argument for continue_report.",
                details={"action": "continue_report", "keys": ["base_report_path", "baseReportPath"]},
            )
        try:
            return self._cvekit_client.continue_report(base_report_path=base_report_path)
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
        base_report_path = self._get_string(payload, "base_report_path", "baseReportPath") or config["current_report_path"]
        working_report_path = self._get_string(payload, "working_report_path", "workingReportPath")
        row = payload.get("row")
        if not base_report_path:
            raise DomainError(
                code="BACKPORT_ARGUMENT_REQUIRED",
                message="Missing required argument for recheck_conflict.",
                details={"action": "recheck_conflict", "keys": ["base_report_path", "baseReportPath"]},
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
            target_path = self._resolve_target_path(payload, config, operation="load_git_log")
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
            target_path = self._resolve_target_path(payload, config, operation="load_git_show")
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
        except (DomainError, RuntimeError, FileNotFoundError, NotADirectoryError) as error:
            logger.exception("load_git_show failed")
            return {
                "operation": "load_git_show",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_execute_selected(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._extract_config(payload)
        base_report_path = self._require_string(payload, "execute_selected", "base_report_path", "baseReportPath")
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
                target_path=self._resolve_target_path(payload, config, operation="execute_selected"),
                patch_dataset_dir=config["patch_dataset_dir"],
                signer_name=config["signer_name"],
                signer_email=config["signer_email"],
                commit_message_template=config["commit_message_template"],
                commit_message_source=config["commit_message_source"],
                linux_repo_path=config["linux_repo_path"],
                working_report_path=working_report_path,
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
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
        base_report_path = self._require_string(payload, "apply_row", "base_report_path", "baseReportPath")
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
        base_report_path = self._require_string(payload, "check_row", "base_report_path", "baseReportPath")
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
            )
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            logger.exception("check_row failed")
            failed_row = dict(row)
            failed_row["status"] = "failed"
            failed_row["error"] = str(error)
            failed_row["conflict_check_error"] = str(error)
            try:
                merged = self._cvekit_client.merge_rows_into_report(base_report_path, [failed_row])
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
        base_report_path = self._require_string(payload, "try_resolve", "base_report_path", "baseReportPath")
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
                target_path=self._resolve_target_path(payload, config, operation="try_resolve"),
                patch_dataset_dir=config["patch_dataset_dir"],
                signer_name=config["signer_name"],
                signer_email=config["signer_email"],
                commit_message_template=config["commit_message_template"],
                commit_message_source=config["commit_message_source"],
                linux_repo_path=config["linux_repo_path"],
                working_report_path=working_report_path,
                target_config_layout=config["target_config_layout"],
                target_config_layout_opts=config["target_config_layout_opts"],
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
        template_override = self._get_string(payload, "commit_message_template", "commitMessageTemplate")
        try:
            return self._cvekit_client.preview_commit_message(
                base_report_path=base_report_path,
                row=row,
                commit_message_template=template_override or config["commit_message_template"],
                commit_message_source=config["commit_message_source"],
                linux_repo_path=config["linux_repo_path"],
                working_report_path=working_report_path,
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
            target_path = self._resolve_target_path(payload, config, operation="check_manual_patch")
            patch_text = self._require_string(payload, "check_manual_patch", "patch_text", "patchText")
            result = self._git_client.check_manual_patch(target_path, patch_text)
            ok = result["returncode"] == "0"
            return {
                "operation": "check_manual_patch",
                "status": "success" if ok else "failed",
                "summary": "手动 Patch 可以干净应用" if ok else "手动 Patch 检查失败",
                "manual_patch": result,
                "diagnostics": {"error_text": result["stderr"]} if not ok else {},
            }
        except (DomainError, RuntimeError, FileNotFoundError, NotADirectoryError, ValueError) as error:
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
            target_path = self._resolve_target_path(payload, config, operation="apply_manual_patch")
            patch_text = self._require_string(payload, "apply_manual_patch", "patch_text", "patchText")
            result = self._git_client.apply_manual_patch(target_path, patch_text)
            ok = result["returncode"] == "0"
            return {
                "operation": "apply_manual_patch",
                "status": "success" if ok else "failed",
                "summary": "手动 Patch 已应用到目标仓" if ok else "手动 Patch 应用失败",
                "manual_patch": result,
                "diagnostics": {"error_text": result["stderr"]} if not ok else {},
            }
        except (DomainError, RuntimeError, FileNotFoundError, NotADirectoryError, ValueError) as error:
            logger.exception("apply_manual_patch failed")
            return {
                "operation": "apply_manual_patch",
                "status": "failed",
                "summary": str(error),
                "diagnostics": {"error_text": str(error)},
            }

    def _run_load_patch_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        base_report_path = self._require_string(payload, "load_patch_preview", "base_report_path", "baseReportPath")
        working_report_path = self._get_string(
            payload,
            "working_report_path",
            "workingReportPath",
            "current_filtered_report_path",
            "currentFilteredReportPath",
        )
        patch_kind = self._require_string(payload, "load_patch_preview", "patch_kind", "patchKind")
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
    def _normalize_layout_fields(config: dict[str, Any], source: dict[str, Any]) -> None:
        """校验并合并 source 中的 layout 字段到 config。

        合法值：写入 config。source 中显式传入但非法：重置为安全默认。
        source 中未包含该 key：保持 config 现有值。
        """
        for key in ("target_config_layout", "target_config_layout_opts"):
            if key not in source:
                continue
            value = source[key]
            if key == "target_config_layout":
                if isinstance(value, str) and value.strip() in {"none", "anolis"}:
                    config[key] = value.strip()
                elif isinstance(value, str) and value.strip():
                    # 显式传入了非法值 → 重置为安全默认
                    config[key] = "none"
                    config["target_config_layout_opts"] = {"default_level": "L1-RECOMMEND"}
                continue
            if key == "target_config_layout_opts":
                if isinstance(value, dict):
                    try:
                        validated = TargetConfigLayoutOpts(**value)
                    except Exception:
                        # 非法 opts → 重置为默认
                        config[key] = {"default_level": "L1-RECOMMEND"}
                        continue
                    merged = dict(config[key])
                    merged.update(validated.model_dump())
                    config[key] = merged
                continue

    def _extract_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_config = payload.get("config")
        normalized = self.get_config()
        if not isinstance(raw_config, dict):
            return normalized

        for key in _default_config():
            if key in ("target_config_layout", "target_config_layout_opts"):
                continue
            value = raw_config.get(key)
            if isinstance(value, str):
                stripped = value.strip()
                if stripped or key.startswith("current_"):
                    normalized[key] = stripped
        self._normalize_layout_fields(normalized, raw_config)
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
            config["current_excel_path"] = self._get_string(payload, "excel_path", "excelPath")
            if action == "generate_report":
                config["current_filtered_report_path"] = ""

        report_path = (
            self._get_string(artifacts, "base_report_path")
            or self._get_string(artifacts, "report_path")
            or self._get_string(report, "report_path")
        )
        if action in {"generate_report", "continue_report", "recheck_conflict", "try_resolve", "run_all"} and report_path:
            config["current_report_path"] = report_path

        filtered_report_path = self._get_string(artifacts, "filtered_report_path")
        if action in {"execute_selected", "run_all"} and filtered_report_path:
            config["current_filtered_report_path"] = filtered_report_path
        self.update_config(config)

    def _emit_run_all_progress(
        self,
        *,
        phase: str,
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
        try:
            self._progress_callback(progress)
        except Exception:
            logger.exception("Backport run_all progress callback failed")

    @staticmethod
    def _is_skipped_commit(item: dict[str, Any]) -> bool:
        status = str(item.get("status") or "").strip().lower()
        merged = str(item.get("merged_in_target") or "").strip().lower()
        return status == "skipped" or merged == "skipped" or item.get("is_merge_commit") is True

    @classmethod
    def _find_blocking_conflict(cls, commits: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in commits
                if isinstance(item, dict)
                and item.get("has_conflict") is True
                and str(item.get("status") or "").strip().lower() not in {"failed", "error"}
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
        return [item for item in commits if isinstance(item, dict)] if isinstance(commits, list) else []

    @staticmethod
    def _describe_commit_row(row: dict[str, Any]) -> str:
        for key in ("row_id", "commit", "input_commit", "title", "commit_title"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "<unknown>"

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
        return value if value in {"auto", "openEuler", "upstream"} else "auto"

    def _resolve_target_path(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
        *,
        operation: str,
    ) -> str:
        target_path = self._get_string(payload, "target_path", "targetPath") or config["target_path"]
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
        return "<backport_result>\n" + json.dumps(parsed_result, ensure_ascii=False, indent=2) + "\n</backport_result>"

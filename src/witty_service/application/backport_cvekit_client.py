from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from witty_service.application.backport_git_client import BackportGitClient


class BackportCvekitClient:
    REFRESH_META_SCHEMA_VERSION = 1
    PATCH_KIND_TO_KEY = {
        "original": "original_patch_path",
        "current": "patch_path",
        "backported": "backported_patch_path",
    }
    PATCH_KEYS = tuple(PATCH_KIND_TO_KEY.values())

    def __init__(
        self,
        *,
        runs_root: str | Path,
        openclaw_config_path: str | Path | None = None,
    ) -> None:
        self._runs_root = Path(runs_root).expanduser().resolve()
        self._openclaw_config_path = self._resolve_openclaw_config_path(openclaw_config_path)

    # ── 初始化 ──────────────────────────────────────────────────

    @staticmethod
    def _resolve_cvekit() -> Path:
        result = subprocess.run(
            ["which", "cvekit"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        candidate = (result.stdout or "").strip()
        if result.returncode == 0 and candidate:
            path = Path(candidate).expanduser().resolve()
            if path.exists():
                return path
        raise RuntimeError("cvekit 不在 PATH 中")

    def _resolve_openclaw_config_path(self, config_path: str | Path | None) -> Path:
        from witty_service.config import get_settings
        path = config_path or get_settings().openclaw.config_path or "~/.openclaw/openclaw.json"
        return Path(path).expanduser().resolve(strict=False)

    # ── cvekit MCP 配置 ────────────────────────────────────────

    def _get_cvekit_mcp_config(self) -> tuple[list[Any], dict[str, Any]]:
        config_path = self._openclaw_config_path
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                config = json.load(handle)
        except Exception as error:
            raise RuntimeError(f"读取 openclaw.json 失败: {error}") from error
        if not isinstance(config, dict):
            raise RuntimeError(f"openclaw.json 顶层必须是对象: {config_path}")

        mcp_config = ((config.get("mcp") or {}).get("servers") or {}).get("cvekit_mcp")
        if not isinstance(mcp_config, dict):
            mcp_config = ((config.get("mcpServers") or {}).get("cvekit_mcp") or {})
        if not isinstance(mcp_config, dict):
            raise RuntimeError(f"openclaw.json 中缺少 cvekit_mcp 配置: {config_path}")

        args = mcp_config.get("args") or []
        if not isinstance(args, list):
            args = []
        env_config = mcp_config.get("env") or {}
        if not isinstance(env_config, dict):
            env_config = {}
        return args, env_config

    @staticmethod
    def _parse_option_values(args: list[Any], option_names: set[str]) -> dict[str, str]:
        arg_values: dict[str, str] = {}
        index = 0
        while index < len(args):
            item = str(args[index]).strip()
            if item in option_names and index + 1 < len(args):
                value = str(args[index + 1]).strip()
                if value:
                    arg_values[item] = value
                index += 2
                continue
            for option_name in option_names:
                prefix = f"{option_name}="
                if item.startswith(prefix):
                    value = item[len(prefix) :].strip()
                    if value:
                        arg_values[option_name] = value
                    break
            index += 1
        return arg_values

    def _get_llm_config(
        self,
        arg_values: dict[str, str],
        env_config: dict[str, Any],
    ) -> dict[str, str]:
        selected_provider = (
            arg_values.get("--llm-provider")
            or str(env_config.get("LLM_PROVIDER") or "").strip()
        ).lower()
        api_key = (
            arg_values.get("--api-key")
            or str(env_config.get("API_KEY") or "").strip()
            or str(env_config.get("OPENAI_KEY") or "").strip()
        )
        base_url = (
            arg_values.get("--llm-base-url")
            or str(env_config.get("LLM_BASE_URL") or "").strip()
            or str(env_config.get("BASE_URL") or "").strip()
        )
        model_name = (
            arg_values.get("--llm-model-name")
            or str(env_config.get("LLM_MODEL_NAME") or "").strip()
            or str(env_config.get("MODEL_NAME") or "").strip()
        )
        if not selected_provider:
            raise RuntimeError(
                "openclaw.json cvekit_mcp 缺少 --llm-provider 或 LLM_PROVIDER: "
                f"{self._openclaw_config_path}"
            )
        if not api_key:
            raise RuntimeError(
                "openclaw.json cvekit_mcp 缺少 --api-key 或 API_KEY: "
                f"{self._openclaw_config_path}"
            )

        return {
            "provider": selected_provider,
            "api_key": api_key,
            "base_url": base_url,
            "model_name": model_name,
        }

    # ── 通用工具 ────────────────────────────────────────────────

    def _build_env(self, mcp_env: dict[str, Any]) -> dict[str, str]:
        cvekit_bin_dir = self._resolve_cvekit().parent
        env: dict[str, str] = {
            "PATH": os.pathsep.join(
                [
                    str(cvekit_bin_dir),
                    "/usr/local/bin",
                    "/usr/bin",
                    "/usr/local/sbin",
                    "/usr/sbin",
                ]
            ),
        }
        for key in ("LANG", "LINUX_REPO_USE_CACHE_ONLY"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        joern_path = str(
            mcp_env.get("JOERN_PATH") or os.environ.get("JOERN_PATH") or ""
        ).strip()
        if joern_path:
            env["JOERN_PATH"] = joern_path
        return env

    def _run_cvekit(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        cmd_args = list(args)
        existing_options = {
            item.split("=", 1)[0]
            for item in cmd_args
            if isinstance(item, str) and item.startswith("--")
        }
        mcp_args, mcp_env = self._get_cvekit_mcp_config()
        mcp_options = self._parse_option_values(
            mcp_args,
            {
                "--llm-provider",
                "--api-key",
                "--llm-base-url",
                "--llm-model-name",
                "--backport-engine",
                "--format-mode",
            },
        )
        llm_config = self._get_llm_config(mcp_options, mcp_env)
        env = self._build_env(mcp_env)
        for option, value in (
            ("--llm-provider", llm_config.get("provider")),
            ("--llm-base-url", llm_config.get("base_url")),
            ("--llm-model-name", llm_config.get("model_name")),
            ("--api-key", llm_config.get("api_key")),
            ("--backport-engine", mcp_options.get("--backport-engine")),
            ("--format-mode", mcp_options.get("--format-mode")),
        ):
            if value and option not in existing_options:
                cmd_args.extend([option, value])

        cmd = [str(self._resolve_cvekit()), *cmd_args]
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if result.returncode != 0:
            redacted_cmd = self._redact_command(cmd)
            raise RuntimeError(
                "cvekit 执行失败\n"
                f"command: {' '.join(redacted_cmd)}\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )
        return result

    @staticmethod
    def _redact_command(cmd: list[str]) -> list[str]:
        redacted: list[str] = []
        skip_next = False
        for item in cmd:
            if skip_next:
                redacted.append("***")
                skip_next = False
                continue
            if item == "--api-key":
                redacted.append(item)
                skip_next = True
                continue
            if item.startswith("--api-key="):
                redacted.append("--api-key=***")
                continue
            redacted.append(item)
        return redacted

    @staticmethod
    def _parse_json_output(output: str) -> dict[str, Any]:
        text = (output or "").strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                return {}
            try:
                loaded = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _read_report(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise RuntimeError(f"report 内容不是合法 YAML 对象: {path}")
        data: dict[str, Any] = loaded
        commits = data.get("commits") or []
        if not isinstance(commits, list):
            commits = []
        return data, commits

    @staticmethod
    def _build_patch_meta(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
        patches: dict[str, dict[str, Any]] = {}
        for kind, key in BackportCvekitClient.PATCH_KIND_TO_KEY.items():
            raw_path = str(item.get(key) or "").strip()
            patch_file = Path(raw_path).expanduser() if raw_path else None
            patches[kind] = {
                "exists": bool(patch_file and patch_file.is_file()),
                "file_name": Path(raw_path).name if raw_path else "",
            }
        return patches

    @staticmethod
    def sanitize_commit_item(item: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(item)
        for key in BackportCvekitClient.PATCH_KEYS:
            sanitized.pop(key, None)
        sanitized["row_id"] = BackportCvekitClient._build_row_id(item)
        sanitized["patches"] = BackportCvekitClient._build_patch_meta(item)
        return sanitized

    @staticmethod
    def sanitize_commit_list(commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            BackportCvekitClient.sanitize_commit_item(item)
            for item in commits
            if isinstance(item, dict)
        ]

    @staticmethod
    def _overlay_commit(raw_row: dict[str, Any], row_overlay: dict[str, Any]) -> dict[str, Any]:
        merged = dict(raw_row)
        for key, value in row_overlay.items():
            if key in {"row_id", "patches"}:
                continue
            if key in BackportCvekitClient.PATCH_KEYS:
                continue
            merged[key] = value
        return merged

    def _resolve_commit_row(
        self,
        *,
        row: dict[str, Any],
        base_report_path: str,
        working_report_path: str | None = None,
    ) -> dict[str, Any]:
        target_row_id = self._build_row_id(row)
        candidate_paths: list[Path] = []
        for raw_path in (working_report_path, base_report_path):
            if not raw_path:
                continue
            resolved = Path(raw_path).expanduser().resolve()
            if resolved in candidate_paths:
                continue
            candidate_paths.append(resolved)

        for candidate in candidate_paths:
            if not candidate.exists():
                continue
            _, commits = self._read_report(candidate)
            for item in commits:
                if not isinstance(item, dict):
                    continue
                if self._build_row_id(item) == target_row_id:
                    return self._overlay_commit(item, row)

        searched = ", ".join(str(path) for path in candidate_paths) or "<empty>"
        raise ValueError(f"report 中找不到 row_id={target_row_id}，searched={searched}")

    # ── report 对齐和元信息 ───────────────────────────────────

    def _mark_merged_by_subject(
        self,
        report_data: dict[str, Any],
        subject_map: dict[str, str],
    ) -> int:
        commits = report_data.get("commits")
        if not isinstance(commits, list) or not commits:
            return 0

        marked_count = 0
        for item in commits:
            if not isinstance(item, dict):
                continue
            title = str(item.get("commit_title") or "").strip()
            matched_commit = subject_map.get(title) if title else None
            if not matched_commit:
                continue
            if item.get("merged_in_target") is not True:
                marked_count += 1
            item["merged_in_target"] = True
            item["merged_check_error"] = None
            item["has_conflict"] = False
            item["conflict_check_method"] = "target-log-subject"
            item["conflict_check_error"] = None
            item["status"] = "success"
            item["error"] = None
            if not str(item.get("applied_commit") or "").strip():
                item["applied_commit"] = matched_commit
        report_data["commits"] = commits
        return marked_count

    def _reconcile_report(self, report_data: dict[str, Any], target_path: str) -> dict[str, Any]:
        commits = report_data.get("commits")
        if not isinstance(commits, list) or not commits:
            return report_data
        subject_map = BackportGitClient.collect_subject_map(target_path)
        if not subject_map:
            return report_data
        self._mark_merged_by_subject(report_data, subject_map)
        return report_data

    def _write_refresh_meta(
        self,
        report_data: dict[str, Any],
        target_state: dict[str, Any],
        *,
        mode: str,
        checked_count: int,
        skipped_count: int,
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "schema_version": self.REFRESH_META_SCHEMA_VERSION,
            "target_path": target_state.get("target_path") or "",
            "target_branch": target_state.get("target_branch") or "",
            "target_head_checked": target_state.get("target_head") or "",
            "target_status_clean": bool(target_state.get("target_status_clean")),
            "refresh_mode": mode,
            "checked_count": checked_count,
            "skipped_count": skipped_count,
            "checked_at": int(time.time()),
        }
        if fallback_reason:
            meta["fallback_reason"] = fallback_reason
        report_data["refresh_meta"] = meta
        return report_data

    @staticmethod
    def _is_skipped_row(row: dict[str, Any]) -> bool:
        status = str(row.get("status") or "").strip().lower()
        merged = str(row.get("merged_in_target") or "").strip().lower()
        return status == "skipped" or merged == "skipped" or row.get("is_merge_commit") is True

    @classmethod
    def _is_blocking_conflict(cls, row: dict[str, Any]) -> bool:
        status = str(row.get("status") or "").strip().lower()
        return row.get("has_conflict") is True and status not in {"failed", "error"} and not cls._is_skipped_row(row)

    @staticmethod
    def _is_pending_row(row: dict[str, Any]) -> bool:
        return str(row.get("status") or "").strip().lower() == "pending"

    @staticmethod
    def _write_report_config(path: Path, report_data: dict[str, Any], commits: list[dict[str, Any]]) -> None:
        config_data = {key: value for key, value in report_data.items() if key != "commits"}
        config_data.pop("api_key", None)
        config_data["commits"] = commits
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config_data, handle, allow_unicode=True, sort_keys=False)

    def _run_stop_at_first_conflict_report(
        self,
        *,
        report_data: dict[str, Any],
        commits: list[dict[str, Any]],
        run_prefix: str,
    ) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
        self._runs_root.mkdir(parents=True, exist_ok=True)
        run_dir = Path(
            tempfile.mkdtemp(
                prefix=f"{run_prefix}_{int(time.time())}_",
                dir=str(self._runs_root),
            )
        )
        report_config_path = run_dir / f"{run_prefix}.report.yml"
        self._write_report_config(report_config_path, report_data, commits)

        self._run_cvekit(
            [
                "--action", "backport-batch",
                "--backport-config", str(report_config_path),
                "--debug", "--json",
                "--stop-at-first-conflict",
            ],
            run_dir,
        )
        updated_report_data, updated_commits = self._read_report(report_config_path)
        return run_dir, updated_report_data, updated_commits

    @classmethod
    def _merge_report_rows(
        cls,
        commits: list[dict[str, Any]],
        updated_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updates = {cls._build_row_id(row): row for row in updated_rows if isinstance(row, dict)}
        return [
            updates.get(cls._build_row_id(row), row)
            for row in commits
            if isinstance(row, dict)
        ]

    @staticmethod
    def _write_report(path: Path, report_data: dict[str, Any], commits: list[dict[str, Any]]) -> None:
        next_report = dict(report_data)
        next_report["commits"] = commits
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(next_report, handle, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _filtered_report_path(base_path: Path) -> Path:
        report_suffix = ".report.yml"
        if base_path.name.endswith(report_suffix):
            return base_path.with_name(f"{base_path.name[:-len(report_suffix)]}.filtered.report.yml")
        return base_path.with_name(f"{base_path.stem}.filtered{base_path.suffix}")

    @staticmethod
    def _infer_likely_missing_prerequisite(text: str) -> bool:
        lowered = text.lower()
        return any(
            keyword in lowered
            for keyword in (
                "missing prerequisite",
                "prerequisite",
                "depends on",
                "already exists in working directory",
                "patch does not apply",
            )
        )

    # ── 生成报告 ────────────────────────────────────────────────

    def generate_report(
        self,
        excel_path: str,
        project_url: str,
        project_dir: str,
        source_branch: str,
        target_path: str,
        target_release: str,
        patch_dataset_dir: str,
        signer_name: str,
        signer_email: str,
        commit_message_template: str,
        commit_message_source: str,
        linux_repo_path: str,
        commit_sort: str = "describe",
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        excel = Path(excel_path).expanduser().resolve()
        if not excel.exists():
            raise FileNotFoundError(f"excel_path 不存在: {excel}")
        excel_suffix = excel.suffix.lower()
        if excel_suffix not in {".xlsx", ".xls"}:
            raise ValueError(f"excel_path 不是 Excel 文件: {excel}")

        target_repo = Path(target_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(target_repo)

        self._runs_root.mkdir(parents=True, exist_ok=True)
        run_dir = Path(
            tempfile.mkdtemp(
                prefix=f"{os.getpid()}_{time.time_ns()}_",
                dir=str(self._runs_root),
            )
        )
        base_config_path = run_dir / "backport.base.yml"
        config_path = run_dir / "backport-batch.yml"
        report_path = run_dir / "backport-batch.yml.report.yml"

        base_config: dict[str, Any] = {
            "project": "linux",
            "target_path": str(target_repo),
        }
        for key, value in {
            "project_url": project_url,
            "project_dir": project_dir,
            "source_branch": source_branch,
            "target_release": target_release,
            "patch_dataset_dir": patch_dataset_dir,
            "signer_name": signer_name,
            "signer_email": signer_email,
            "commit_message_template": commit_message_template,
            "commit_message_source": self._normalize_commit_message_source(commit_message_source),
            "linux_repo_path": linux_repo_path,
            "commit_sort": commit_sort,
        }.items():
            if isinstance(value, str) and value.strip():
                base_config[key] = value.strip()

        # 仅在 layout != "none" 时写入新字段
        if target_config_layout and target_config_layout != "none":
            base_config["target_config_layout"] = target_config_layout
            if target_config_layout_opts and isinstance(target_config_layout_opts, dict):
                base_config["target_config_layout_opts"] = target_config_layout_opts

        with base_config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(base_config, handle, allow_unicode=True, sort_keys=False)

        self._run_cvekit(
            ["--action", "backport-batch",
             "--backport-excel", str(excel),
             "-o", str(config_path),
             "--backport-config", str(base_config_path)],
            run_dir,
        )

        self._run_cvekit(
            ["--action", "backport-batch",
            "--backport-config", str(config_path),
            "--debug", "--json",
            "--stop-at-first-conflict"],
            run_dir,
        )

        if not report_path.exists():
            raise RuntimeError(f"cvekit 执行后未生成报告文件: {report_path}")

        report_data, commits = self._read_report(report_path)
        report_data = self._reconcile_report(report_data, str(target_repo))
        target_state = BackportGitClient.get_repo_state(str(target_repo))
        self._write_refresh_meta(
            report_data,
            target_state,
            mode="generate-report",
            checked_count=len(commits),
            skipped_count=0,
        )
        with report_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(report_data, handle, allow_unicode=True, sort_keys=False)

        _, commits = self._read_report(report_path)

        return {
            "operation": "generate_report",
            "status": "success",
            "stage": "interactive_editing",
            "summary": f"生成报告成功，共 {len(commits)} 条 commit",
            "artifacts": {
                "report_path": str(report_path),
                "config_path": str(config_path),
                "base_config_path": str(base_config_path),
                "run_dir": str(run_dir),
            },
            "report": {
                "report_path": str(report_path),
                "commit_count": len(commits),
                "commits": commits,
            },
        }

    def continue_report(
        self,
        base_report_path: str,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")

        report_data, commits = self._read_report(base_path)
        if not commits:
            raise RuntimeError(f"report 中没有可继续检查的 commits: {base_path}")

        blocking_conflict = next(
            (row for row in commits if isinstance(row, dict) and self._is_blocking_conflict(row)),
            None,
        )
        if blocking_conflict is not None:
            return {
                "operation": "continue_report",
                "status": "failed",
                "stage": "interactive_editing",
                "summary": "当前仍有阻塞冲突，请先检测或处理当前冲突后再继续检查。",
                "artifacts": {"base_report_path": str(base_path)},
                "report": {
                    "report_path": str(base_path),
                    "commit_count": len(commits),
                    "commits": commits,
                },
            }

        first_pending_index = next(
            (idx for idx, row in enumerate(commits) if isinstance(row, dict) and self._is_pending_row(row)),
            None,
        )
        if first_pending_index is None:
            return {
                "operation": "continue_report",
                "status": "success",
                "stage": "interactive_editing",
                "summary": "当前 report 没有待检查的 pending 条目。",
                "artifacts": {"base_report_path": str(base_path)},
                "report": {
                    "report_path": str(base_path),
                    "commit_count": len(commits),
                    "commits": commits,
                },
            }

        run_dir, _, updated_commits = self._run_stop_at_first_conflict_report(
            report_data=report_data,
            commits=commits,
            run_prefix="continue-backport-batch",
        )
        self._write_report(base_path, report_data, updated_commits)
        _, persisted_commits = self._read_report(base_path)
        return {
            "operation": "continue_report",
            "status": "success",
            "stage": "interactive_editing",
            "summary": f"继续检查完成，从第 {first_pending_index + 1} 条 pending 开始推进。",
            "artifacts": {
                "base_report_path": str(base_path),
                "run_dir": str(run_dir),
            },
            "report": {
                "report_path": str(base_path),
                "commit_count": len(persisted_commits),
                "commits": persisted_commits,
            },
        }

    def load_report(self, base_report_path: str) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")
        _, commits = self._read_report(base_path)
        return {
            "operation": "load_report",
            "status": "success",
            "stage": "interactive_editing",
            "artifacts": {"base_report_path": str(base_path)},
            "report": {
                "report_path": str(base_path),
                "commit_count": len(commits),
                "commits": commits,
            },
        }

    def pin_target_title_index_baseline(
        self,
        *,
        base_report_path: str,
        target_path: str,
    ) -> str:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")
        target_state = BackportGitClient.get_repo_state(target_path)
        target_head = str(target_state.get("target_head") or "").strip()
        if not target_head:
            raise RuntimeError(f"无法解析目标仓 HEAD: {target_path}")

        report_data, commits = self._read_report(base_path)
        # 一键运行期间 target 会不断前进；这个字段只固定“标题索引”的查询基线，
        # 不影响后续 git apply/cherry-pick 使用当前 HEAD。
        report_data["target_title_index_ref_sha"] = target_head
        self._write_report(base_path, report_data, commits)
        return target_head

    def check_row(
        self,
        base_report_path: str,
        row: dict[str, Any],
        working_report_path: str | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")

        report_data, commits = self._read_report(base_path)
        resolved_row = self._resolve_commit_row(
            row=row,
            base_report_path=base_report_path,
            working_report_path=working_report_path,
        )

        # 主 report 是可恢复的工作表；单行临时 report 只用于检查当前 commit。
        # 每次检查结束后都把单行结果 merge 回主 report，避免整表预检查。
        row_for_check = dict(resolved_row)
        original_patch_path = str(
            row_for_check.get("original_patch_path") or row_for_check.get("patch_path") or ""
        ).strip()
        row_for_check["status"] = "pending"
        row_for_check["merged_in_target"] = None
        row_for_check["merged_check_error"] = None
        row_for_check["has_conflict"] = None
        row_for_check["conflict_check_method"] = None
        row_for_check["conflict_check_error"] = None
        row_for_check["backported_patch_path"] = None
        if original_patch_path:
            row_for_check["original_patch_path"] = original_patch_path
            row_for_check["patch_path"] = original_patch_path

        run_dir: Path | None = None
        try:
            run_dir, _, updated_rows = self._run_stop_at_first_conflict_report(
                report_data=report_data,
                commits=[row_for_check],
                run_prefix="check-backport-row",
            )
            updated_row = updated_rows[0] if updated_rows else row_for_check
            next_commits = self._merge_report_rows(commits, [updated_row])
            self._write_report(base_path, report_data, next_commits)
            return {
                "operation": "check_row",
                "status": "success",
                "stage": "interactive_editing",
                "summary": "当前提交已检查。",
                "artifacts": {
                    "base_report_path": str(base_path),
                },
                "report": {
                    "report_path": str(base_path),
                    "commit_count": 1,
                    "commits": [updated_row],
                },
            }
        finally:
            if run_dir is not None:
                shutil.rmtree(run_dir, ignore_errors=True)

    def merge_rows_into_report(
        self,
        base_report_path: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")
        report_data, commits = self._read_report(base_path)
        next_commits = self._merge_report_rows(commits, rows)
        self._write_report(base_path, report_data, next_commits)
        row_ids = {self._build_row_id(row) for row in rows if isinstance(row, dict)}
        affected_rows = [
            item
            for item in next_commits
            if isinstance(item, dict) and self._build_row_id(item) in row_ids
        ]
        return {
            "operation": "merge_rows",
            "status": "success",
            "stage": "interactive_editing",
            "artifacts": {"base_report_path": str(base_path)},
            "report": {
                "report_path": str(base_path),
                "commit_count": len(affected_rows),
                "commits": affected_rows,
            },
        }

    def recheck_conflict(
        self,
        base_report_path: str,
        row: dict[str, Any],
        working_report_path: str | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")

        report_data, commits = self._read_report(base_path)
        first_conflict = next(
            (item for item in commits if isinstance(item, dict) and self._is_blocking_conflict(item)),
            None,
        )
        if first_conflict is None:
            return {
                "operation": "recheck_conflict",
                "status": "failed",
                "stage": "interactive_editing",
                "summary": "当前 report 没有可检测的阻塞冲突。",
                "artifacts": {"base_report_path": str(base_path)},
                "report": {"commit_count": 0, "commits": []},
            }

        resolved_row = self._resolve_commit_row(
            row=row,
            base_report_path=base_report_path,
            working_report_path=working_report_path,
        )
        target_row_id = self._build_row_id(resolved_row)
        if self._build_row_id(first_conflict) != target_row_id:
            return {
                "operation": "recheck_conflict",
                "status": "failed",
                "stage": "interactive_editing",
                "summary": "只能检测当前第一条阻塞冲突。",
                "artifacts": {"base_report_path": str(base_path)},
                "report": {"commit_count": 1, "commits": [first_conflict]},
            }

        row_for_check = dict(resolved_row)
        original_patch_path = str(
            row_for_check.get("original_patch_path") or row_for_check.get("patch_path") or ""
        ).strip()
        row_for_check["status"] = "pending"
        row_for_check["merged_in_target"] = None
        row_for_check["merged_check_error"] = None
        row_for_check["has_conflict"] = None
        row_for_check["conflict_check_method"] = None
        row_for_check["conflict_check_error"] = None
        row_for_check["backported_patch_path"] = None
        if original_patch_path:
            row_for_check["original_patch_path"] = original_patch_path
            row_for_check["patch_path"] = original_patch_path

        run_dir, _, updated_rows = self._run_stop_at_first_conflict_report(
            report_data=report_data,
            commits=[row_for_check],
            run_prefix="recheck-backport-conflict",
        )
        updated_row = updated_rows[0] if updated_rows else row_for_check
        next_commits = self._merge_report_rows(commits, [updated_row])
        self._write_report(base_path, report_data, next_commits)
        return {
            "operation": "recheck_conflict",
            "status": "success",
            "stage": "interactive_editing",
            "summary": "当前冲突已重新检测。",
            "artifacts": {
                "base_report_path": str(base_path),
                "run_dir": str(run_dir),
            },
            "report": {
                "report_path": str(base_path),
                "commit_count": 1,
                "commits": [updated_row],
            },
        }

    # ── 执行选中 commit ────────────────────────────────────────

    def execute_selected(
        self,
        base_report_path: str,
        selected_commits: list[dict[str, Any]],
        target_path: str,
        patch_dataset_dir: str,
        signer_name: str,
        signer_email: str,
        commit_message_template: str,
        commit_message_source: str,
        linux_repo_path: str,
        working_report_path: str | None = None,
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")

        orig_report, _ = self._read_report(base_path)
        if not selected_commits:
            raise ValueError("selected_commits 为空")

        filtered_report_path = self._filtered_report_path(base_path)

        resolved_commits = [
            self._resolve_commit_row(
                row=item,
                base_report_path=base_report_path,
                working_report_path=working_report_path,
            )
            for item in selected_commits
            if isinstance(item, dict)
        ]
        if not resolved_commits:
            raise ValueError("selected_commits 解析后为空")

        actionable_commits = [
            item
            for item in resolved_commits
            if item.get("merged_in_target") is not True
            and item.get("empty_patch") is not True
            and item.get("equivalent_exists") is not True
            and str(item.get("status") or "").strip().lower() != "skipped"
            and str(item.get("merged_in_target") or "").strip().lower() != "skipped"
        ]
        if not actionable_commits:
            return {
                "operation": "execute_selected",
                "status": "success",
                "stage": "interactive_editing",
                "summary": f"选中的 {len(resolved_commits)} 条 commit 均无需执行",
                "report": {
                    "commit_count": len(resolved_commits),
                    "commits": resolved_commits,
                },
                "diagnostics": {
                    "likely_missing_prerequisite": False,
                },
            }

        config_data = dict(orig_report)
        config_data["commits"] = actionable_commits
        config_data.pop("api_key", None)
        if patch_dataset_dir.strip():
            config_data["patch_dataset_dir"] = patch_dataset_dir.strip()
        if signer_name.strip():
            config_data["signer_name"] = signer_name.strip()
        if signer_email.strip():
            config_data["signer_email"] = signer_email.strip()
        if target_path.strip():
            config_data["target_path"] = target_path.strip()
        if commit_message_template.strip():
            config_data["commit_message_template"] = commit_message_template
        commit_message_source = self._normalize_commit_message_source(commit_message_source)
        if commit_message_source != "auto":
            config_data["commit_message_source"] = commit_message_source
        if linux_repo_path.strip():
            config_data["linux_repo_path"] = linux_repo_path.strip()

        # 仅在 layout != "none" 时写入新字段
        if target_config_layout and target_config_layout != "none":
            config_data["target_config_layout"] = target_config_layout
            if target_config_layout_opts and isinstance(target_config_layout_opts, dict):
                config_data["target_config_layout_opts"] = target_config_layout_opts

        with filtered_report_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config_data, handle, allow_unicode=True, sort_keys=False)

        cmd = [
            "--action", "backport-batch",
            "--backport-config", str(filtered_report_path),
            "-e", "--debug", "--json",
        ]
        result = self._run_cvekit(cmd, filtered_report_path.parent)
        combined_output = "\n".join(part for part in [result.stdout, result.stderr] if part)

        _, commits = self._read_report(filtered_report_path)
        return {
            "operation": "execute_selected",
            "status": "success",
            "stage": "interactive_editing",
            "summary": f"执行完成，共处理 {len(commits)} 条 commit",
            "artifacts": {
                "filtered_report_path": str(filtered_report_path),
            },
            "report": {
                "commit_count": len(commits),
                "commits": commits,
            },
            "diagnostics": {
                "likely_missing_prerequisite": self._infer_likely_missing_prerequisite(combined_output),
            },
        }

    @staticmethod
    def _normalize_try_resolve_row(row: dict[str, Any]) -> dict[str, Any]:
        updated = dict(row)
        backported_patch_path = str(updated.get("backported_patch_path") or "").strip()
        applied_commit = str(updated.get("applied_commit") or "").strip()
        status = str(updated.get("status") or "").strip().lower()
        if status == "success" and applied_commit:
            updated["has_conflict"] = False
            updated["merged_in_target"] = True
            updated["conflict_check_error"] = None
            updated["error"] = None
            return updated
        if status == "success" and backported_patch_path and not applied_commit:
            updated["has_conflict"] = False
            updated["conflict_check_method"] = "backport-generated"
            updated["conflict_check_error"] = None
            updated["error"] = None
            updated["patch_path"] = backported_patch_path
        return updated

    def try_resolve(
        self,
        base_report_path: str,
        row: dict[str, Any],
        target_path: str,
        patch_dataset_dir: str,
        signer_name: str,
        signer_email: str,
        commit_message_template: str,
        commit_message_source: str,
        linux_repo_path: str,
        working_report_path: str | None = None,
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")

        _, base_commits = self._read_report(base_path)
        first_conflict = next(
            (item for item in base_commits if isinstance(item, dict) and self._is_blocking_conflict(item)),
            None,
        )
        if first_conflict is None:
            return {
                "operation": "try_resolve",
                "status": "failed",
                "stage": "interactive_editing",
                "summary": "当前 report 没有可处理的阻塞冲突。",
                "artifacts": {"base_report_path": str(base_path)},
                "report": {"commit_count": 0, "commits": []},
                "diagnostics": {},
            }

        resolved_row = self._resolve_commit_row(
            row=row,
            base_report_path=base_report_path,
            working_report_path=working_report_path,
        )
        if self._build_row_id(first_conflict) != self._build_row_id(resolved_row):
            return {
                "operation": "try_resolve",
                "status": "failed",
                "stage": "interactive_editing",
                "summary": "只能处理当前第一条阻塞冲突。",
                "artifacts": {"base_report_path": str(base_path)},
                "report": {"commit_count": 1, "commits": [first_conflict]},
                "diagnostics": {},
            }

        result = self.execute_selected(
            base_report_path=base_report_path,
            selected_commits=[resolved_row],
            target_path=target_path,
            patch_dataset_dir=patch_dataset_dir,
            signer_name=signer_name,
            signer_email=signer_email,
            commit_message_template=commit_message_template,
            commit_message_source=commit_message_source,
            linux_repo_path=linux_repo_path,
            working_report_path=working_report_path,
            target_config_layout=target_config_layout,
            target_config_layout_opts=target_config_layout_opts,
        )
        affected_rows = [
            self._normalize_try_resolve_row(item)
            for item in result.get("report", {}).get("commits", [])
            if isinstance(item, dict)
        ]
        if affected_rows:
            report_data, commits = self._read_report(base_path)
            next_commits = self._merge_report_rows(commits, affected_rows)
            self._write_report(base_path, report_data, next_commits)
            _, persisted_commits = self._read_report(base_path)
            affected_ids = {self._build_row_id(row) for row in affected_rows}
            affected_rows = [
                item
                for item in persisted_commits
                if self._build_row_id(item) in affected_ids
            ]

        artifacts = dict(result.get("artifacts") or {})
        artifacts["base_report_path"] = str(base_path)
        return {
            "operation": "try_resolve",
            "status": result.get("status") or "success",
            "stage": "interactive_editing",
            "summary": result.get("summary") or "冲突处理完成",
            "artifacts": artifacts,
            "report": {
                "report_path": str(base_path),
                "commit_count": len(affected_rows),
                "commits": affected_rows,
            },
            "diagnostics": result.get("diagnostics") or {},
        }

    # ── 单条 apply ─────────────────────────────────────────────

    def apply_row(
        self,
        base_report_path: str,
        row: dict[str, Any],
        commit_message_template: str,
        commit_message_source: str,
        signer_name: str,
        signer_email: str,
        linux_repo_path: str,
        working_report_path: str | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")
        apply_config_path = base_path
        if working_report_path:
            candidate_apply_path = Path(working_report_path).expanduser().resolve()
            if candidate_apply_path.exists():
                apply_config_path = candidate_apply_path

        resolved_row = self._resolve_commit_row(
            row=row,
            base_report_path=base_report_path,
            working_report_path=working_report_path,
        )
        if (
            resolved_row.get("merged_in_target") is True
            or resolved_row.get("empty_patch") is True
            or resolved_row.get("equivalent_exists") is True
            or str(resolved_row.get("status") or "").strip().lower() == "skipped"
            or str(resolved_row.get("merged_in_target") or "").strip().lower() == "skipped"
        ):
            return {
                "operation": "apply_row",
                "status": "success",
                "stage": "interactive_editing",
                "summary": "该提交无需执行",
                "report": {"commit_count": 1, "commits": [resolved_row]},
            }
        apply_value = self._resolve_apply_value(resolved_row)
        target_row_id = self._build_row_id(resolved_row)
        self._override_commit_message_config(
            apply_config_path,
            commit_message_template=commit_message_template,
            commit_message_source=commit_message_source,
            signer_name=signer_name,
            signer_email=signer_email,
            linux_repo_path=linux_repo_path,
        )

        result = self._run_cvekit(
            ["--action", "backport-batch",
             "--backport-config", str(apply_config_path), "--debug", "--json",
             "--apply", apply_value],
            apply_config_path.parent,
        )
        apply_result = self._parse_json_output(result.stdout)

        _, commits = self._read_report(apply_config_path)
        affected_rows = [c for c in commits if isinstance(c, dict) and self._build_row_id(c) == target_row_id] or [resolved_row]
        apply_status = str(apply_result.get("status") or "").strip().lower()
        if apply_status and apply_status != "success":
            affected_rows = [
                {
                    **row,
                    "status": apply_status,
                    "error": apply_result.get("error") or row.get("error"),
                    "conflict_check_method": row.get("conflict_check_method") or "apply",
                    "conflict_check_error": apply_result.get("error") or row.get("conflict_check_error"),
                }
                for row in affected_rows
            ]

        if affected_rows:
            report_data, base_commits = self._read_report(base_path)
            next_commits = self._merge_report_rows(base_commits, affected_rows)
            self._write_report(base_path, report_data, next_commits)
            affected_ids = {self._build_row_id(row) for row in affected_rows}
            affected_rows = [
                item
                for item in next_commits
                if self._build_row_id(item) in affected_ids
            ]

        return {
            "operation": "apply_row",
            "status": "failed" if apply_status == "failed" else "success",
            "stage": "interactive_editing",
            "summary": apply_result.get("error") or "单条应用执行完成",
            "report": {"commit_count": len(affected_rows), "commits": affected_rows},
        }

    def preview_commit_message(
        self,
        base_report_path: str,
        row: dict[str, Any],
        commit_message_template: str,
        commit_message_source: str,
        linux_repo_path: str,
        working_report_path: str | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")
        preview_config_path = base_path
        if working_report_path:
            candidate_path = Path(working_report_path).expanduser().resolve()
            if candidate_path.exists():
                preview_config_path = candidate_path

        resolved_row = self._resolve_commit_row(
            row=row,
            base_report_path=base_report_path,
            working_report_path=working_report_path,
        )
        apply_value = self._resolve_apply_value(resolved_row)
        cmd = [
            "--action", "backport-batch",
            "--backport-config", str(preview_config_path),
            "--debug", "--json",
            "--preview-commit-message",
            "--apply", apply_value,
        ]
        if commit_message_template.strip():
            cmd.extend(["--commit-message-template", commit_message_template])
        commit_message_source = self._normalize_commit_message_source(commit_message_source)
        if commit_message_source != "auto":
            cmd.extend(["--commit-message-source", commit_message_source])
        if linux_repo_path.strip():
            cmd.extend(["--linux-repo-path", linux_repo_path.strip()])
        result = self._run_cvekit(cmd, preview_config_path.parent)
        preview_result = self._parse_json_output(result.stdout)
        if preview_result.get("status") != "success":
            raise RuntimeError(str(preview_result.get("error") or preview_result))
        return {
            "operation": "preview_commit_message",
            "status": "success",
            "summary": "commit message 预览已生成",
            "commit_message": {
                "message": preview_result.get("commit_message") or preview_result.get("commit_message_preview") or "",
                "context": preview_result.get("commit_message_context") or {},
                "source_detection": preview_result.get("source_detection") or {},
                "warnings": preview_result.get("commit_message_warnings") or [],
            },
        }

    def load_patch_preview(
        self,
        *,
        base_report_path: str,
        row: dict[str, Any],
        patch_kind: str,
        working_report_path: str | None = None,
    ) -> dict[str, Any]:
        if patch_kind not in self.PATCH_KIND_TO_KEY:
            raise ValueError(f"不支持的 patch_kind: {patch_kind}")

        resolved_row = self._resolve_commit_row(
            row=row,
            base_report_path=base_report_path,
            working_report_path=working_report_path,
        )
        patch_path = str(resolved_row.get(self.PATCH_KIND_TO_KEY[patch_kind]) or "").strip()
        if not patch_path:
            raise FileNotFoundError(f"{patch_kind} patch 不存在")

        patch_file = Path(patch_path).expanduser().resolve()
        if not patch_file.exists():
            raise FileNotFoundError(f"patch 文件不存在: {patch_file}")

        patch_text = patch_file.read_text(encoding="utf-8")
        return {
            "operation": "load_patch_preview",
            "status": "success",
            "patch": {
                "kind": patch_kind,
                "file_name": patch_file.name,
                "patch_text": patch_text,
                "size_bytes": patch_file.stat().st_size,
            },
        }

    @staticmethod
    def _override_commit_message_config(
        config_path: Path,
        *,
        commit_message_template: str,
        commit_message_source: str,
        signer_name: str,
        signer_email: str,
        linux_repo_path: str,
    ) -> None:
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                config_data = yaml.safe_load(handle) or {}
        except FileNotFoundError:
            raise
        if not isinstance(config_data, dict):
            raise ValueError(f"backport 配置不是对象: {config_path}")
        if commit_message_template.strip():
            config_data["commit_message_template"] = commit_message_template
        commit_message_source = BackportCvekitClient._normalize_commit_message_source(commit_message_source)
        if commit_message_source != "auto":
            config_data["commit_message_source"] = commit_message_source
        if signer_name.strip():
            config_data["signer_name"] = signer_name.strip()
        if signer_email.strip():
            config_data["signer_email"] = signer_email.strip()
        if linux_repo_path.strip():
            config_data["linux_repo_path"] = linux_repo_path.strip()
        with config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config_data, handle, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _resolve_apply_value(row: dict[str, Any]) -> str:
        for key in ("backported_patch_path", "patch_path", "original_patch_path", "commit", "input_commit"):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        raise ValueError(f"row 中缺少可用于 apply 的字段: {list(row.keys())}")

    @staticmethod
    def _build_row_id(row: dict[str, Any]) -> str:
        for key in ("row_id", "commit", "input_commit", "original_patch_path", "patch_path", "backported_patch_path"):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return json.dumps(row, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _normalize_commit_message_source(value: str) -> str:
        return value if value in {"auto", "openEuler", "upstream"} else "auto"

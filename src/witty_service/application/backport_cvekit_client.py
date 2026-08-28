from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml

from witty_service.application.backport_git_client import BackportGitClient
from witty_service.application.backport_run_store import BackportRunStore

logger = logging.getLogger(__name__)

# 目标仓库跨进程锁等待上限(秒);超时发布 repository_lock_timeout 并中止任务
REPOSITORY_LOCK_TIMEOUT_SECONDS = 600


@dataclass(slots=True)
class BackportRuntimeConfig:
    llm_provider: str
    api_key: str
    llm_base_url: str = ""
    llm_model_name: str = ""
    backport_engine: str = "opencode"
    format_mode: str = "changed"


class BackportCvekitClient:
    REFRESH_META_SCHEMA_VERSION = 1
    CVEKIT_OPTION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
    CVEKIT_OPTION_DENYLIST: ClassVar[set[str]] = {
        "action",
        "api_key",
        "apply",
        "backport_config",
        "backport_engine",
        "backport_excel",
        "branch",
        "clone_dir",
        "commit_message_source",
        "commit_message_template",
        "debug",
        "excel_sheet",
        "execute",
        "format_mode",
        "fork_repo_url",
        "gitee_token",
        "json",
        "linux_repo_path",
        "llm_base_url",
        "llm_model_name",
        "llm_provider",
        "output",
        "patch_dataset_dir",
        "preview_commit_message",
        "project_dir",
        "project_url",
        "repo_url",
        "signer_email",
        "signer_name",
        "source_branch",
        "stop_at_first_conflict",
        "target_config_layout",
        "target_config_layout_opts",
        "target_path",
        "target_release",
    }
    PATCH_KIND_TO_KEY: ClassVar[dict[str, str]] = {
        "original": "original_patch_path",
        "current": "patch_path",
        "backported": "backported_patch_path",
    }
    PATCH_KEYS = tuple(PATCH_KIND_TO_KEY.values())

    def __init__(
        self,
        *,
        runs_root: str | Path,
        patchflow_state_root: str | Path | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._runs_root = Path(runs_root).expanduser().resolve()
        self._run_store = BackportRunStore(
            self._runs_root, patchflow_state_root=patchflow_state_root
        )
        self._conflict_reporter_url = ""
        self._runtime_config: BackportRuntimeConfig | None = None
        self._progress_callback = progress_callback
        self._archive_run_id = ""
        self._lock_target = ""
        self._lock_held = 0

    # ── 初始化 ──────────────────────────────────────────────────

    @staticmethod
    def resolve_cvekit_path() -> Path:
        result = subprocess.run(
            ["which", "cvekit"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        candidate = (result.stdout or "").strip()
        if result.returncode == 0 and candidate:
            path = Path(candidate).expanduser().resolve()
            if path.exists():
                return path
        raise RuntimeError("cvekit 不在 PATH 中")

    def set_conflict_reporter_url(self, url: str) -> None:
        self._conflict_reporter_url = url.strip()

    def set_lock_target(self, target_path: str | None) -> None:
        """设置目标仓库锁上下文:_run_cvekit 执行期间自动加跨进程 flock。

        同一规范化 target_path 的并发 cvekit 执行互斥(覆盖整个执行生命周期);
        竞争时经 progress_callback 发布 repository_lock_waiting/acquired/timeout 事件。
        由 service 层在 run 操作入口设置、finally 清除。
        """
        self._lock_target = str(target_path or "").strip()

    # ── 目标仓库跨进程锁 ────────────────────────────────────────

    @staticmethod
    def _read_lock_owner(lock_path: Path) -> dict[str, str]:
        """读取锁文件中的 owner 信息(task_id/operation),供竞争方展示。"""
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: str(v) for k, v in data.items() if v}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _publish_lock_event(self, event: dict[str, Any]) -> None:
        """发布仓库锁事件(经 progress_callback,与 MR 172 事件协议一致)。"""
        if self._progress_callback is not None:
            try:
                self._progress_callback(event)
            except Exception:
                # 事件发布失败不得中断任务
                logger.warning(
                    "发布仓库锁事件失败: %s", event.get("event"), exc_info=True
                )

    @contextlib.contextmanager
    def repository_lock(self):
        """对 _lock_target 加跨进程 flock,覆盖整个 cvekit 执行生命周期。

        公开 API:service 层跨类调用前必须先 set_lock_target()(时序耦合
        由调用方保证,见 backport_service.run_action);可重入:service 层
        在整个 run 操作期间持有(覆盖多次 cvekit 调用与 git 操作),
        _run_cvekit 等嵌套进入时直接通过,不重复加锁。
        竞争时发布 repository_lock_waiting(带 owner 与剩余超时)事件,
        阻塞轮询等待;获得锁发布 acquired,超时发布 timeout 并抛错。
        无竞争直接获得时也发布 acquired(service 侧对无竞争获取忽略展示)。
        """
        if not self._lock_target:
            yield
            return
        if self._lock_held > 0:
            # 可重入:同进程内已持有(service 层全程持有)
            self._lock_held += 1
            try:
                yield
            finally:
                self._lock_held -= 1
            return
        lock_root = self._run_store.locks_root
        lock_root.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(
            str(Path(self._lock_target).expanduser().resolve()).encode()
        ).hexdigest()[:16]
        lock_path = lock_root / f"{key}.lock"
        owner_task = getattr(self._run_store, "_run_id", "") or ""
        timeout_seconds = REPOSITORY_LOCK_TIMEOUT_SECONDS
        self._lock_held = 1
        try:
            with open(lock_path, "a+", encoding="utf-8") as lock_file:
                owner: dict[str, str] = {}
                waited = 0
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    # 竞争:读 owner,发布 waiting,阻塞轮询等待(带超时)
                    owner = self._read_lock_owner(lock_path)
                    started = time.monotonic()
                    self._publish_lock_event(
                        {
                            "event": "repository_lock_waiting",
                            "wait_seconds": 0,
                            "timeout_seconds": timeout_seconds,
                            "owner": owner,
                        }
                    )
                    while True:
                        try:
                            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except OSError:
                            waited = int(time.monotonic() - started)
                            if waited >= timeout_seconds:
                                self._publish_lock_event(
                                    {
                                        "event": "repository_lock_timeout",
                                        "wait_seconds": waited,
                                        "timeout_seconds": timeout_seconds,
                                        "owner": owner,
                                    }
                                )
                                raise RuntimeError(
                                    f"等待目标仓库锁超时({timeout_seconds}s): {self._lock_target}"
                                ) from exc
                            self._publish_lock_event(
                                {
                                    "event": "repository_lock_waiting",
                                    "wait_seconds": waited,
                                    "timeout_seconds": timeout_seconds,
                                    "owner": owner,
                                }
                            )
                            time.sleep(0.5)
                # 已获得锁:写入 owner 信息并发布 acquired
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(
                    json.dumps(
                        {
                            "task_id": owner_task,
                            "operation": self._lock_target,
                            "acquired_at": time.time(),
                        }
                    )
                )
                lock_file.flush()
                self._publish_lock_event(
                    {
                        "event": "repository_lock_acquired",
                        "wait_seconds": waited,
                        "timeout_seconds": timeout_seconds,
                        "owner": {
                            "task_id": owner_task,
                            "operation": self._lock_target,
                        },
                    }
                )
                try:
                    yield
                finally:
                    # 先清空 owner(仍持锁),再释放:避免竞争者已写入的新 owner 被清空
                    try:
                        lock_file.seek(0)
                        lock_file.truncate()
                        lock_file.write("{}")
                        lock_file.flush()
                    except OSError:
                        pass
                    with contextlib.suppress(OSError):
                        fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            self._lock_held = 0

    def set_runtime_config(self, runtime_config: BackportRuntimeConfig | None) -> None:
        self._runtime_config = runtime_config
        self._run_store.set_secrets(
            [runtime_config.api_key]
            if runtime_config and runtime_config.api_key
            else []
        )

    def set_archive_run_id(self, run_id: str | None) -> None:
        self._run_store.set_run_id(run_id)
        self._archive_run_id = self._run_store.safe_slug(run_id or "", fallback="")

    def archive_artifacts_for_report(self, report_path: str) -> dict[str, str]:
        base_path = Path(report_path).expanduser().resolve()
        run_dir = self._run_store.ensure_for_report(base_path)
        return self._run_store.artifacts(run_dir)

    @classmethod
    def build_cvekit_option_args(
        cls,
        cvekit_options: dict[str, Any] | None,
    ) -> list[str]:
        if not cvekit_options:
            return []
        args: list[str] = []
        for key, value in cvekit_options.items():
            normalized_key = str(key).strip().replace("-", "_")
            if not cls.CVEKIT_OPTION_KEY_PATTERN.match(normalized_key):
                raise ValueError(f"cvekit option 名称不合法: {key}")
            if normalized_key in cls.CVEKIT_OPTION_DENYLIST:
                raise ValueError(f"cvekit option 由后端管理，不能覆盖: {key}")
            cli_name = f"--{normalized_key.replace('_', '-')}"
            if value is True:
                args.append(cli_name)
            elif value is False or value is None or value == "":
                continue
            elif isinstance(value, (dict, list)):
                args.extend([cli_name, json.dumps(value, ensure_ascii=False)])
            elif isinstance(value, (str, int, float)):
                args.extend([cli_name, str(value)])
            else:
                raise ValueError(f"cvekit option 类型不支持: {key}")
        return args

    # ── 通用工具 ────────────────────────────────────────────────

    def _close_failed_run(self, run_ctx) -> None:
        """登记记录异常收尾:标记 failed 并写 ended_at,避免永久遗留 running。"""
        self._run_store.complete_cvekit_run(run_ctx, [], "failed")

    @staticmethod
    def _action_from_args(args: list[str]) -> str:
        """从 cvekit 命令行提取 action,供 task 日志 metadata 的 operation。"""
        for index, item in enumerate(args):
            if item.startswith("--action="):
                return item.split("=", 1)[1]
            if item == "--action" and index + 1 < len(args):
                return args[index + 1]
        return "cvekit"

    def _build_env(self, run_id: str | None = None) -> dict[str, str]:
        cvekit_bin_dir = self.resolve_cvekit_path().parent
        env: dict[str, str] = {
            "PATH": os.pathsep.join(
                [
                    str(cvekit_bin_dir),
                    str(Path.home() / ".npm-global" / "bin"),
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
        joern_path = os.environ.get("JOERN_PATH", "").strip()
        if joern_path:
            env["JOERN_PATH"] = joern_path
        conflict_reporter_url = (
            self._conflict_reporter_url
            or os.environ.get("CONFLICT_REPORTER_URL", "").strip()
        )
        if conflict_reporter_url:
            env["CONFLICT_REPORTER_URL"] = conflict_reporter_url
        if self._runtime_config is not None:
            env["API_KEY"] = self._runtime_config.api_key
            env["LLM_PROVIDER"] = self._runtime_config.llm_provider
            if self._runtime_config.llm_base_url:
                env["LLM_BASE_URL"] = self._runtime_config.llm_base_url
            if self._runtime_config.llm_model_name:
                env["LLM_MODEL_NAME"] = self._runtime_config.llm_model_name
        # 注入本次 cvekit run 的独立 run_id 与统一日志目录:cvekit 据此落盘;
        # CVEKIT_TASK_ID(稳定 task 标识)由 _run_cvekit 补充
        if run_id:
            env["CVEKIT_RUN_ID"] = run_id
        env["CVEKIT_LOG_DIR"] = str(self._run_store.logs_root)
        return env

    def _run_cvekit(
        self,
        args: list[str],
        cwd: Path,
        operation: str | None = None,
        *,
        require_runtime_config: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if require_runtime_config and self._runtime_config is None:
            raise RuntimeError("Backport 运行环境未配置，请在 Backport 配置区选择运行模型。")
        cmd_args = list(args)
        existing_options = {
            item.split("=", 1)[0]
            for item in cmd_args
            if isinstance(item, str) and item.startswith("--")
        }
        runtime_config = self._runtime_config
        # 每次调用独立 cvekit run ID(启动前登记,子进程崩溃也可恢复);
        # CVEKIT_TASK_ID 保持 task 级稳定标识
        task_id = getattr(self._run_store, "_run_id", "") or ""
        run_ctx = self._run_store.begin_cvekit_run(
            task_id, operation or self._action_from_args(cmd_args)
        )
        try:
            env = self._build_env(run_ctx.cvekit_run_id if run_ctx else None)
        except Exception:
            # 启动前异常(build_env/resolve_cvekit_path 等):关闭登记记录
            self._close_failed_run(run_ctx)
            raise
        if self._archive_run_id:
            env["CVEKIT_TASK_ID"] = self._archive_run_id
        if runtime_config is not None:
            for option, value in (
                ("--llm-provider", runtime_config.llm_provider),
                ("--llm-base-url", runtime_config.llm_base_url),
                ("--llm-model-name", runtime_config.llm_model_name),
                ("--backport-engine", runtime_config.backport_engine),
                ("--format-mode", runtime_config.format_mode),
            ):
                if value and option not in existing_options:
                    cmd_args.extend([option, value])

        try:
            cmd = [str(self.resolve_cvekit_path()), *cmd_args]
        except Exception:
            self._close_failed_run(run_ctx)
            raise
        progress_path = cwd / f".cvekit-progress-{uuid.uuid4().hex}.json"
        env["CVEKIT_PROGRESS_FILE"] = str(progress_path)
        stop_progress = threading.Event()
        progress_thread: threading.Thread | None = None

        if self._progress_callback is not None:
            callback = self._progress_callback
            last_progress_payload = [""]

            def publish_progress() -> None:
                try:
                    payload_text = progress_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return
                if payload_text == last_progress_payload[0]:
                    return
                last_progress_payload[0] = payload_text
                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError:
                    return
                if isinstance(payload, dict):
                    try:
                        callback(payload)
                    except Exception:
                        # Progress reporting must never terminate cvekit.
                        return

            def monitor_progress() -> None:
                while not stop_progress.wait(0.5):
                    publish_progress()

            progress_thread = threading.Thread(
                target=monitor_progress,
                daemon=True,
                name=f"cvekit-progress-{uuid.uuid4().hex[:8]}",
            )
            progress_thread.start()

        try:
            with self.repository_lock():
                result = subprocess.run(
                    cmd,
                    cwd=str(cwd),
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
        except Exception:
            self._close_failed_run(run_ctx)
            raise
        finally:
            stop_progress.set()
            if progress_thread is not None:
                progress_thread.join(timeout=1)
                publish_progress()
            progress_path.unlink(missing_ok=True)
        try:
            source_log_archive_dir = self._run_store.write_command_logs(
                cwd=cwd,
                command=self._redact_command(cmd),
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )
            source_log_root = self._run_store.logs_root
            source_log_paths = {
                Path(value).expanduser().resolve()
                for value in re.findall(
                    r"""(/[^\s"',]+\.log)""",
                    "\n".join((result.stdout or "", result.stderr or "")),
                )
            }
            for source_log in sorted(source_log_paths):
                if (
                    not source_log.is_relative_to(source_log_root)
                    or not source_log.name.startswith("patchflow-")
                    or not source_log.is_file()
                ):
                    continue
                # 新命名(patchflow-{ts}-{run_id8}-...)文件名已不含引擎段,归档时
                # 以 {engine_dir}- 前缀保留引擎信息;batch 目录主日志保留原名,
                # 供 report 的 log_path 字段按原名回退查找
                engine_dir = source_log.parent.name
                archived_name = (
                    f"{engine_dir}-{source_log.name}"
                    if engine_dir and engine_dir != "batch"
                    else source_log.name
                )
                archived_log = source_log_archive_dir / archived_name
                cleaned = self._run_store.redact(
                    source_log.read_text(encoding="utf-8", errors="replace"),
                    [self._runtime_config.api_key] if self._runtime_config else None,
                )
                self._run_store.write_text(archived_log, cleaned)
                if not archived_log.is_file():
                    raise RuntimeError(f"Backport 日志归档失败: {source_log}")
            # task 级日志聚合(独立于 attempt 归档,失败不阻断业务)
            self._run_store.complete_cvekit_run(
                run_ctx,
                sorted(source_log_paths),
                "success" if result.returncode == 0 else "failed",
            )
        except Exception:
            self._close_failed_run(run_ctx)
            raise
        if result.returncode != 0:
            redacted_cmd = self._redact_command(cmd)
            secrets = [runtime_config.api_key] if runtime_config and runtime_config.api_key else []
            raise RuntimeError(
                "cvekit 执行失败\n"
                f"command: {' '.join(redacted_cmd)}\n"
                f"stdout: {self._run_store.redact(result.stdout or '', secrets)}\n"
                f"stderr: {self._run_store.redact(result.stderr or '', secrets)}"
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
        with path.open("r", encoding="utf-8", errors="replace") as handle:
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
                "exists": bool(
                    patch_file
                    and patch_file.is_file()
                    and patch_file.stat().st_size > 0
                ),
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
    def _overlay_commit(
        raw_row: dict[str, Any], row_overlay: dict[str, Any]
    ) -> dict[str, Any]:
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
            for row_number, item in enumerate(commits, start=1):
                if not isinstance(item, dict):
                    continue
                if self._build_row_id(item) == target_row_id:
                    resolved = self._overlay_commit(item, row)
                    resolved.setdefault("row_number", row_number)
                    return resolved

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
            item["existing_commit"] = matched_commit
        report_data["commits"] = commits
        return marked_count

    def _reconcile_report(
        self, report_data: dict[str, Any], target_path: str
    ) -> dict[str, Any]:
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
        return (
            status == "skipped"
            or merged == "skipped"
            or row.get("is_merge_commit") is True
        )

    @classmethod
    def _is_blocking_conflict(cls, row: dict[str, Any]) -> bool:
        status = str(row.get("status") or "").strip().lower()
        return (
            row.get("has_conflict") is True
            and status not in {"failed", "error"}
            and not cls._is_skipped_row(row)
        )

    @staticmethod
    def _is_pending_row(row: dict[str, Any]) -> bool:
        return str(row.get("status") or "").strip().lower() == "pending"

    @staticmethod
    def _write_report_config(
        path: Path,
        report_data: dict[str, Any],
        commits: list[dict[str, Any]],
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
    ) -> None:
        config_data = {
            key: value for key, value in report_data.items() if key != "commits"
        }
        config_data.pop("api_key", None)
        config_data.pop("target_config_layout", None)
        config_data.pop("target_config_layout_opts", None)
        target_config_layout, target_config_layout_opts = (
            BackportCvekitClient._normalize_layout_fields(
                target_config_layout, target_config_layout_opts
            )
        )
        if target_config_layout and target_config_layout != "none":
            config_data["target_config_layout"] = target_config_layout
            if target_config_layout_opts and isinstance(
                target_config_layout_opts, dict
            ):
                config_data["target_config_layout_opts"] = target_config_layout_opts
        config_data["commits"] = commits
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config_data, handle, allow_unicode=True, sort_keys=False)

    def _run_stop_at_first_conflict_report(
        self,
        *,
        report_data: dict[str, Any],
        commits: list[dict[str, Any]],
        run_prefix: str,
        run_dir: Path | None = None,
        report_name: str | None = None,
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
    ) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
        if run_dir is None:
            self._runs_root.mkdir(parents=True, exist_ok=True)
            run_dir = Path(
                tempfile.mkdtemp(
                    prefix=f"{run_prefix}_{int(time.time())}_",
                    dir=str(self._runs_root),
                )
            )
        else:
            run_dir.mkdir(parents=True, exist_ok=True)
        report_config_path = run_dir / (report_name or f"{run_prefix}.report.yml")
        self._write_report_config(
            report_config_path,
            report_data,
            commits,
            target_config_layout=target_config_layout,
            target_config_layout_opts=target_config_layout_opts,
        )

        self._run_cvekit(
            [
                "--action",
                "backport-batch",
                "--backport-config",
                str(report_config_path),
                "--debug",
                "--json",
                "--stop-at-first-conflict",
            ],
            run_dir,
            operation="stop_at_first_conflict_report",
        )
        updated_report_data, updated_commits = self._read_report(report_config_path)
        return run_dir, updated_report_data, updated_commits

    @classmethod
    def _merge_report_rows(
        cls,
        commits: list[dict[str, Any]],
        updated_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updates = {
            cls._build_row_id(row): row for row in updated_rows if isinstance(row, dict)
        }
        return [
            updates.get(cls._build_row_id(row), row)
            for row in commits
            if isinstance(row, dict)
        ]

    def _write_report(
        self,
        path: Path,
        report_data: dict[str, Any],
        commits: list[dict[str, Any]],
    ) -> None:
        if self._run_store.is_report_frozen(path):
            raise RuntimeError("已结束 Run 的报告不可修改，请创建新 Run。")
        next_report = dict(report_data)
        next_report["commits"] = commits
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(next_report, handle, allow_unicode=True, sort_keys=False)
        temp_path.replace(path)

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

    def _write_base_config(
        self,
        path: Path,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        base_config: dict[str, Any] = {
            "project": "linux",
            "target_path": str(config.get("target_path") or ""),
        }
        normalized_message_source = self._normalize_commit_message_source(
            str(config.get("commit_message_source") or "upstream")
        )
        for key, value in {
            "project_url": config.get("project_url"),
            "project_dir": config.get("project_dir"),
            "source_branch": config.get("source_branch"),
            "target_release": config.get("target_release"),
            "patch_dataset_dir": config.get("patch_dataset_dir"),
            "signer_name": config.get("signer_name"),
            "signer_email": config.get("signer_email"),
            "commit_message_template": config.get("commit_message_template"),
            "commit_message_source": normalized_message_source,
            "commit_sort": config.get("commit_sort"),
        }.items():
            if isinstance(value, str) and value.strip():
                base_config[key] = value.strip()
        target_config_layout, target_config_layout_opts = self._normalize_layout_fields(
            str(config.get("target_config_layout") or "none"),
            dict(config["target_config_layout_opts"])
            if isinstance(config.get("target_config_layout_opts"), dict)
            else None,
        )
        if target_config_layout and target_config_layout != "none":
            base_config["target_config_layout"] = target_config_layout
            if target_config_layout_opts:
                base_config["target_config_layout_opts"] = target_config_layout_opts
        linux_repo_path = str(config.get("linux_repo_path") or "").strip()
        if normalized_message_source == "auto" and linux_repo_path:
            base_config["linux_repo_path"] = linux_repo_path
        with path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(base_config, handle, allow_unicode=True, sort_keys=False)
        return base_config

    @classmethod
    def _merge_prerequisite_commits(
        cls,
        config_path: Path,
        prerequisite_commits: list[dict[str, Any]],
    ) -> int:
        """把用户确认的前置提交注入 raw batch config，按完整 sha 去重后返回新增条数。"""
        data, commits = cls._read_report(config_path)
        selected: list[dict[str, Any]] = []
        for candidate in prerequisite_commits:
            if not isinstance(candidate, dict) or not candidate.get("commit"):
                continue
            selected.append(
                {
                    "commit": str(candidate["commit"]),
                    "commit_title": str(candidate.get("title") or ""),
                    "origin": "prerequisite",
                    "required_by": list(candidate.get("required_by") or []),
                    "capabilities": list(candidate.get("capabilities") or []),
                }
            )
        existing = {
            str(row.get("commit"))
            for row in commits
            if isinstance(row, dict) and row.get("commit")
        }
        added: list[dict[str, Any]] = []
        seen = set(existing)
        for row in selected:
            key = str(row["commit"])
            if key in seen:
                continue
            seen.add(key)
            added.append(row)
        if added:
            next_report = dict(data)
            next_report["commits"] = [*commits, *added]
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(next_report, handle, allow_unicode=True, sort_keys=False)
        return len(added)

    def generate_report(
        self,
        excel_path: str | None,
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
        prerequisite_commits: list[dict[str, Any]] | None = None,
        commit_entries: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        if bool(excel_path) == (commit_entries is not None):
            raise ValueError("generate_report 必须且只能使用 Excel 或提交清单作为输入。")
        excel: Path | None = None
        if excel_path:
            excel = Path(excel_path).expanduser().resolve()
            if not excel.exists():
                raise FileNotFoundError(f"excel_path 不存在: {excel}")
            excel_suffix = excel.suffix.lower()
            if excel_suffix not in {".xlsx", ".xls"}:
                raise ValueError(f"excel_path 不是 Excel 文件: {excel}")

        target_repo = Path(target_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(target_repo)

        run_dir = self._run_store.create_run_dir(
            operation="generate_report",
            request={
                "excel_path": str(excel) if excel is not None else "",
                "commit_entries": commit_entries,
                "project_url": project_url,
                "project_dir": project_dir,
                "source_branch": source_branch,
                "target_path": str(target_repo),
                "target_release": target_release,
                "patch_dataset_dir": patch_dataset_dir,
                "commit_sort": commit_sort,
                "target_config_layout": target_config_layout,
                "target_config_layout_opts": target_config_layout_opts or {},
            },
        )
        if excel is not None:
            self._run_store.archive_excel(run_dir, excel)
        else:
            self._run_store.archive_commit_entries(run_dir, commit_entries or [])
        archive_root = Path(self._run_store.artifacts(run_dir)["run_dir"])
        base_config_path = run_dir / "input" / "backport.base.yml"
        config_path = run_dir / "reports" / "backport-batch.yml"
        report_path = run_dir / "reports" / "backport-batch.yml.report.yml"
        base_config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.parent.mkdir(parents=True, exist_ok=True)

        base_config = self._write_base_config(
            base_config_path,
            {
                "project_url": project_url,
                "project_dir": project_dir,
                "source_branch": source_branch,
                "target_path": str(target_repo),
                "target_release": target_release,
                "patch_dataset_dir": patch_dataset_dir,
                "signer_name": signer_name,
                "signer_email": signer_email,
                "commit_message_template": commit_message_template,
                "commit_message_source": commit_message_source,
                "linux_repo_path": linux_repo_path,
                "commit_sort": commit_sort,
                "target_config_layout": target_config_layout,
                "target_config_layout_opts": target_config_layout_opts or {},
            },
        )
        archived_config_path = archive_root / "input" / "config.json"
        try:
            archived_config = json.loads(
                archived_config_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            archived_config = {}
        if not isinstance(archived_config, dict):
            archived_config = {}
        archived_config.update(base_config)
        if self._runtime_config is not None:
            archived_config["model"] = {
                "record_id": str(archived_config.get("backport_model_id") or ""),
                "display_name": self._runtime_config.llm_model_name,
                "provider": self._runtime_config.llm_provider,
                "base_url": self._runtime_config.llm_base_url,
                "model_name": self._runtime_config.llm_model_name,
                "backport_engine": self._runtime_config.backport_engine,
                "format_mode": self._runtime_config.format_mode,
            }
        self._run_store.write_json(archived_config_path, archived_config)

        if excel is not None:
            self._run_cvekit(
                [
                    "--action",
                    "backport-batch",
                    "--backport-excel",
                    str(excel),
                    "-o",
                    str(config_path),
                    "--backport-config",
                    str(base_config_path),
                ],
                run_dir,
                operation="generate_config",
            )
        else:
            raw_config = {**base_config, "commits": commit_entries or []}
            with config_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(raw_config, handle, allow_unicode=True, sort_keys=False)
            self._run_store.write_text(
                archive_root / "input" / "backport-batch.yml",
                config_path.read_text(encoding="utf-8"),
            )

        if prerequisite_commits:
            self._merge_prerequisite_commits(config_path, prerequisite_commits)

        self._run_cvekit(
            [
                "--action",
                "backport-batch",
                "--backport-config",
                str(config_path),
                "--debug",
                "--json",
                "--stop-at-first-conflict",
            ],
            run_dir,
            operation="generate_report",
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
        initial_report_path = archive_root / "input" / "initial-report.yml"
        self._run_store.write_text(
            initial_report_path,
            report_path.read_text(encoding="utf-8"),
        )
        self._run_store.update_manifest(
            archive_root,
            {
                "status": "ready",
                "current_report": "input/initial-report.yml",
                "summary": {"total": len(commits), "success": 0, "failed": 0},
                "target": {
                    "repository": str(
                        (
                            self._run_store.read_manifest(archive_root).get("target")
                            or {}
                        ).get("repository")
                        or target_repo
                    ),
                    "branch": str(target_state.get("target_branch") or target_release),
                    "head": str(target_state.get("target_head") or ""),
                },
            },
        )

        response = {
            "operation": "generate_report",
            "status": "success",
            "stage": "interactive_editing",
            "summary": f"生成报告成功，共 {len(commits)} 条 commit",
            "artifacts": {
                "report_path": str(initial_report_path),
                **self._run_store.artifacts(run_dir),
            },
            "report": {
                "report_path": str(initial_report_path),
                "commit_count": len(commits),
                "commits": commits,
            },
        }
        self._run_store.cleanup_work_dir(run_dir)
        return response

    def extract_config_commits(
        self,
        excel_path: str,
        config: dict[str, Any],
    ) -> list[dict[str, Any]]:
        excel = Path(excel_path).expanduser().resolve()
        if not excel.exists():
            raise FileNotFoundError(f"excel_path 不存在: {excel}")
        if excel.suffix.lower() not in {".xlsx", ".xls"}:
            raise ValueError(f"excel_path 不是 Excel 文件: {excel}")

        with tempfile.TemporaryDirectory(prefix="witty-backport-prerequisite-") as temp_dir:
            work = Path(temp_dir)
            base_config_path = work / "backport.base.yml"
            config_path = work / "backport-batch.yml"
            self._write_base_config(base_config_path, config)
            self._run_cvekit(
                [
                    "--action",
                    "backport-batch",
                    "--backport-excel",
                    str(excel),
                    "-o",
                    str(config_path),
                    "--backport-config",
                    str(base_config_path),
                ],
                work,
                require_runtime_config=False,
            )
            if not config_path.exists():
                raise RuntimeError(f"cvekit 未生成配置文件: {config_path}")
            _, commits = self._read_report(config_path)
            return commits

    def prerequisite_commits(
        self,
        source_repo: str,
        target_repo: str,
        target_ref: str,
        prereq_commits: list[str],
    ) -> dict[str, Any]:
        args = [
            "--action",
            "prerequisite-commits",
            "--source-repo",
            str(source_repo),
            "--target-repo",
            str(target_repo),
            "--target-ref",
            str(target_ref),
        ]
        for sha in prereq_commits:
            args.extend(["--prereq-commit", str(sha)])
        result = self._run_cvekit(
            args,
            Path(source_repo),
            require_runtime_config=False,
        )
        return self._parse_json_output(result.stdout)

    def continue_report(
        self,
        base_report_path: str,
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")

        report_data, commits = self._read_report(base_path)
        if not commits:
            raise RuntimeError(f"report 中没有可继续检查的 commits: {base_path}")

        blocking_conflict = next(
            (
                row
                for row in commits
                if isinstance(row, dict) and self._is_blocking_conflict(row)
            ),
            None,
        )
        if blocking_conflict is not None:
            archive_run_dir = self._run_store.ensure_for_report(base_path)
            return {
                "operation": "continue_report",
                "status": "failed",
                "stage": "interactive_editing",
                "summary": "当前仍有阻塞冲突，请先检测或处理当前冲突后再继续检查。",
                "artifacts": {
                    "base_report_path": str(base_path),
                    **self._run_store.artifacts(archive_run_dir),
                },
                "report": {
                    "report_path": str(base_path),
                    "commit_count": len(commits),
                    "commits": commits,
                },
            }

        first_pending_index = next(
            (
                idx
                for idx, row in enumerate(commits)
                if isinstance(row, dict) and self._is_pending_row(row)
            ),
            None,
        )
        if first_pending_index is None:
            archive_run_dir = self._run_store.ensure_for_report(base_path)
            return {
                "operation": "continue_report",
                "status": "success",
                "stage": "interactive_editing",
                "summary": "当前 report 没有待检查的 pending 条目。",
                "artifacts": {
                    "base_report_path": str(base_path),
                    **self._run_store.artifacts(archive_run_dir),
                },
                "report": {
                    "report_path": str(base_path),
                    "commit_count": len(commits),
                    "commits": commits,
                },
            }

        archive_run_dir = self._run_store.ensure_for_report(base_path)
        attempt_dir = self._run_store.next_attempt_dir(
            archive_run_dir / "reports" / "continue"
        )
        _, _, updated_commits = self._run_stop_at_first_conflict_report(
            report_data=report_data,
            commits=commits,
            run_prefix="continue-backport-batch",
            run_dir=attempt_dir,
            report_name="report.report.yml",
            target_config_layout=target_config_layout,
            target_config_layout_opts=target_config_layout_opts,
        )
        self._write_report(base_path, report_data, updated_commits)
        _, persisted_commits = self._read_report(base_path)
        if (attempt_dir / "report.report.yml").exists():
            shutil.copy2(attempt_dir / "report.report.yml", attempt_dir / "report.yml")
        manifest_updates: dict[str, Any] = {
            "summary": {
                "total": len(persisted_commits),
                "success": sum(
                    1
                    for row in persisted_commits
                    if str(row.get("status") or "").lower()
                    in {"success", "applied", "resolved"}
                ),
                "failed": sum(
                    1
                    for row in persisted_commits
                    if str(row.get("status") or "").lower() == "failed"
                ),
            },
        }
        if self._archive_run_id:
            manifest_updates["status"] = "running"
        self._run_store.update_manifest(archive_run_dir, manifest_updates)
        previous_rows = {
            self._build_row_id(row): row for row in commits if isinstance(row, dict)
        }
        changed_rows = [
            row
            for row in persisted_commits
            if previous_rows.get(self._build_row_id(row)) != row
        ]
        archived_cases = [
            self._run_store.archive_case_attempt(
                attempt_dir=attempt_dir,
                report_path=attempt_dir / "report.report.yml",
                rows=[row],
                sanitized_rows=[self.sanitize_commit_item(row)],
                patch_keys=self.PATCH_KEYS,
                result={"operation": "continue_report", "status": "success"},
                cleanup=False,
                task_dir=archive_run_dir,
                archive_scope="run" if self._archive_run_id else "interaction",
            )
            for row in changed_rows
        ]
        self._run_store.cleanup_work_dir(attempt_dir)
        return {
            "operation": "continue_report",
            "status": "success",
            "stage": "interactive_editing",
            "summary": f"继续检查完成，从第 {first_pending_index + 1} 条 pending 开始推进。",
            "artifacts": {
                "base_report_path": str(base_path),
                "attempt_dir": str(attempt_dir),
                **self._run_store.artifacts(archive_run_dir),
            },
            "report": {
                "report_path": str(base_path),
                "commit_count": len(persisted_commits),
                "commits": persisted_commits,
            },
            "archive": {"cases": archived_cases},
        }

    def load_report(self, base_report_path: str) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")
        _, commits = self._read_report(base_path)
        archive_run_dir = self._run_store.ensure_for_report(base_path)
        return {
            "operation": "load_report",
            "status": "success",
            "stage": "interactive_editing",
            "artifacts": {
                "base_report_path": str(base_path),
                **self._run_store.artifacts(archive_run_dir),
            },
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
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
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
            row_for_check.get("original_patch_path")
            or row_for_check.get("patch_path")
            or ""
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

        archive_run_dir = self._run_store.ensure_for_report(base_path)
        case_dir = self._run_store.case_dir(
            archive_run_dir,
            row_for_check,
            row_id=self._build_row_id(row_for_check),
        )
        attempt_dir = self._run_store.next_attempt_dir(case_dir)
        _, _, updated_rows = self._run_stop_at_first_conflict_report(
            report_data=report_data,
            commits=[row_for_check],
            run_prefix="check-backport-row",
            run_dir=attempt_dir,
            report_name="report.report.yml",
            target_config_layout=target_config_layout,
            target_config_layout_opts=target_config_layout_opts,
        )
        updated_row = updated_rows[0] if updated_rows else row_for_check
        next_commits = self._merge_report_rows(commits, [updated_row])
        self._write_report(base_path, report_data, next_commits)
        case_result = self._run_store.archive_case_attempt(
            attempt_dir=attempt_dir,
            report_path=attempt_dir / "report.report.yml",
            rows=[updated_row],
            sanitized_rows=[self.sanitize_commit_item(updated_row)],
            patch_keys=self.PATCH_KEYS,
            result={"operation": "check_row", "status": "success"},
            task_dir=archive_run_dir,
            archive_scope="run" if self._archive_run_id else "interaction",
        )
        return {
            "operation": "check_row",
            "status": "success",
            "stage": "interactive_editing",
            "summary": "当前提交已检查。",
            "artifacts": {
                "base_report_path": str(base_path),
                "attempt_dir": str(attempt_dir),
                "case_dir": str(case_dir),
                **self._run_store.artifacts(archive_run_dir),
            },
            "report": {
                "report_path": str(base_path),
                "commit_count": 1,
                "commits": [updated_row],
            },
            "archive": case_result,
        }

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
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")

        report_data, commits = self._read_report(base_path)
        first_conflict = next(
            (
                item
                for item in commits
                if isinstance(item, dict) and self._is_blocking_conflict(item)
            ),
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
            row_for_check.get("original_patch_path")
            or row_for_check.get("patch_path")
            or ""
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

        archive_run_dir = self._run_store.ensure_for_report(base_path)
        case_dir = self._run_store.case_dir(
            archive_run_dir,
            row_for_check,
            row_id=self._build_row_id(row_for_check),
        )
        attempt_dir = self._run_store.next_attempt_dir(case_dir)
        _, _, updated_rows = self._run_stop_at_first_conflict_report(
            report_data=report_data,
            commits=[row_for_check],
            run_prefix="recheck-backport-conflict",
            run_dir=attempt_dir,
            report_name="report.report.yml",
            target_config_layout=target_config_layout,
            target_config_layout_opts=target_config_layout_opts,
        )
        updated_row = updated_rows[0] if updated_rows else row_for_check
        next_commits = self._merge_report_rows(commits, [updated_row])
        self._write_report(base_path, report_data, next_commits)
        case_result = self._run_store.archive_case_attempt(
            attempt_dir=attempt_dir,
            report_path=attempt_dir / "report.report.yml",
            rows=[updated_row],
            sanitized_rows=[self.sanitize_commit_item(updated_row)],
            patch_keys=self.PATCH_KEYS,
            result={"operation": "recheck_conflict", "status": "success"},
            task_dir=archive_run_dir,
            archive_scope="run" if self._archive_run_id else "interaction",
        )
        return {
            "operation": "recheck_conflict",
            "status": "success",
            "stage": "interactive_editing",
            "summary": "当前冲突已重新检测。",
            "artifacts": {
                "base_report_path": str(base_path),
                "attempt_dir": str(attempt_dir),
                "case_dir": str(case_dir),
                **self._run_store.artifacts(archive_run_dir),
            },
            "report": {
                "report_path": str(base_path),
                "commit_count": 1,
                "commits": [updated_row],
            },
            "archive": case_result,
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
        cvekit_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")

        orig_report, _ = self._read_report(base_path)
        if not selected_commits:
            raise ValueError("selected_commits 为空")

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
            archive_run_dir = self._run_store.ensure_for_report(base_path)
            return {
                "operation": "execute_selected",
                "status": "success",
                "stage": "interactive_editing",
                "summary": f"选中的 {len(resolved_commits)} 条 commit 均无需执行",
                "artifacts": {
                    "base_report_path": str(base_path),
                    **self._run_store.artifacts(archive_run_dir),
                },
                "report": {
                    "commit_count": len(resolved_commits),
                    "commits": resolved_commits,
                },
                "diagnostics": {
                    "likely_missing_prerequisite": False,
                },
            }

        archive_run_dir = self._run_store.ensure_for_report(base_path)
        if len(actionable_commits) == 1:
            case_dir = self._run_store.case_dir(
                archive_run_dir,
                actionable_commits[0],
                row_id=self._build_row_id(actionable_commits[0]),
            )
            attempt_dir = self._run_store.next_attempt_dir(case_dir)
        else:
            case_dir = None
            attempt_dir = self._run_store.next_attempt_dir(
                archive_run_dir / "reports" / "execute"
            )
        filtered_report_path = attempt_dir / "report.report.yml"

        config_data = dict(orig_report)
        config_data["commits"] = actionable_commits
        config_data.pop("api_key", None)
        config_data.pop("target_config_layout", None)
        config_data.pop("target_config_layout_opts", None)
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
        commit_message_source = self._normalize_commit_message_source(
            commit_message_source
        )
        if commit_message_source != "auto":
            config_data["commit_message_source"] = commit_message_source
        if commit_message_source == "auto" and linux_repo_path.strip():
            config_data["linux_repo_path"] = linux_repo_path.strip()

        # 校验并归一化 layout 字段
        target_config_layout, target_config_layout_opts = self._normalize_layout_fields(
            target_config_layout, target_config_layout_opts
        )

        # 仅在 layout != "none" 时写入新字段
        if target_config_layout and target_config_layout != "none":
            config_data["target_config_layout"] = target_config_layout
            if target_config_layout_opts and isinstance(
                target_config_layout_opts, dict
            ):
                config_data["target_config_layout_opts"] = target_config_layout_opts

        with filtered_report_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config_data, handle, allow_unicode=True, sort_keys=False)

        cmd = [
            "--action",
            "backport-batch",
            "--backport-config",
            str(filtered_report_path),
            "-e",
            "--debug",
            "--json",
        ]
        cmd.extend(self.build_cvekit_option_args(cvekit_options))
        result = self._run_cvekit(cmd, attempt_dir, operation="execute_selected")
        combined_output = "\n".join(
            part for part in [result.stdout, result.stderr] if part
        )

        _, commits = self._read_report(filtered_report_path)
        report_data, base_commits = self._read_report(base_path)
        merged_commits = self._merge_report_rows(base_commits, commits)
        self._write_report(base_path, report_data, merged_commits)
        commits = [
            item
            for item in merged_commits
            if self._build_row_id(item)
            in {self._build_row_id(updated) for updated in commits}
        ]
        archive_payload: dict[str, Any] | None = None
        if case_dir is not None:
            archive_payload = self._run_store.archive_case_attempt(
                attempt_dir=attempt_dir,
                report_path=filtered_report_path,
                rows=commits,
                sanitized_rows=self.sanitize_commit_list(commits),
                patch_keys=self.PATCH_KEYS,
                result={"operation": "execute_selected", "status": "success"},
                task_dir=archive_run_dir,
                archive_scope="run" if self._archive_run_id else "interaction",
            )
        else:
            archive_payload = {
                "cases": [
                    self._run_store.archive_case_attempt(
                        attempt_dir=attempt_dir,
                        report_path=filtered_report_path,
                        rows=[row],
                        sanitized_rows=[self.sanitize_commit_item(row)],
                        patch_keys=self.PATCH_KEYS,
                        result={"operation": "execute_selected", "status": "success"},
                        cleanup=False,
                        task_dir=archive_run_dir,
                        archive_scope="run" if self._archive_run_id else "interaction",
                    )
                    for row in commits
                ]
            }
            self._run_store.cleanup_work_dir(attempt_dir)
        if self._archive_run_id:
            self._run_store.update_manifest(
                archive_run_dir,
                {"status": "running"},
            )
        return {
            "operation": "execute_selected",
            "status": "success",
            "stage": "interactive_editing",
            "summary": f"执行完成，共处理 {len(commits)} 条 commit",
            "artifacts": {
                "filtered_report_path": str(filtered_report_path),
                "attempt_dir": str(attempt_dir),
                **({"case_dir": str(case_dir)} if case_dir is not None else {}),
                **self._run_store.artifacts(archive_run_dir),
            },
            "report": {
                "commit_count": len(commits),
                "commits": commits,
            },
            "diagnostics": {
                "likely_missing_prerequisite": self._infer_likely_missing_prerequisite(
                    combined_output
                ),
            },
            "archive": archive_payload or {},
        }

    @staticmethod
    def _normalize_try_resolve_row(row: dict[str, Any]) -> dict[str, Any]:
        updated = dict(row)
        if updated.get("equivalent_exists") is True:
            updated["has_conflict"] = False
            updated["conflict_check_method"] = "llm-equivalence"
            updated["conflict_check_error"] = None
            updated["backported_patch_path"] = ""
            updated["patch_path"] = ""
            updated["error"] = None
            return updated
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
        cvekit_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_path = Path(base_report_path).expanduser().resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_report_path 不存在: {base_path}")

        _, base_commits = self._read_report(base_path)
        first_conflict = next(
            (
                item
                for item in base_commits
                if isinstance(item, dict) and self._is_blocking_conflict(item)
            ),
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
            cvekit_options=cvekit_options,
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
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
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
            or str(resolved_row.get("merged_in_target") or "").strip().lower()
            == "skipped"
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
            target_config_layout=target_config_layout,
            target_config_layout_opts=target_config_layout_opts,
        )

        archive_run_dir = self._run_store.ensure_for_report(base_path)
        case_dir = self._run_store.case_dir(
            archive_run_dir,
            resolved_row,
            row_id=self._build_row_id(resolved_row),
        )
        attempt_dir = self._run_store.next_attempt_dir(case_dir)
        execution_report_path = attempt_dir / "apply.report.yml"
        shutil.copy2(apply_config_path, execution_report_path)
        result = self._run_cvekit(
            [
                "--action",
                "backport-batch",
                "--backport-config",
                str(execution_report_path),
                "--debug",
                "--json",
                "--apply",
                apply_value,
            ],
            attempt_dir,
            operation="apply_row",
        )
        apply_result = self._parse_json_output(result.stdout)

        _, commits = self._read_report(execution_report_path)
        affected_rows = [
            c
            for c in commits
            if isinstance(c, dict) and self._build_row_id(c) == target_row_id
        ] or [resolved_row]
        apply_status = str(apply_result.get("status") or "").strip().lower()
        if apply_status and apply_status != "success":
            affected_rows = [
                {
                    **row,
                    "status": apply_status,
                    "error": apply_result.get("error") or row.get("error"),
                    "conflict_check_method": row.get("conflict_check_method")
                    or "apply",
                    "conflict_check_error": apply_result.get("error")
                    or row.get("conflict_check_error"),
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

        case_result = self._run_store.archive_case_attempt(
            attempt_dir=attempt_dir,
            report_path=execution_report_path,
            rows=affected_rows,
            sanitized_rows=self.sanitize_commit_list(affected_rows),
            patch_keys=self.PATCH_KEYS,
            result={
                "operation": "apply_row",
                "status": "failed" if apply_status == "failed" else "success",
                "log_path": apply_result.get("log_path"),
            },
            task_dir=archive_run_dir,
            archive_scope="run" if self._archive_run_id else "interaction",
        )
        if self._archive_run_id:
            self._run_store.update_manifest(
                archive_run_dir,
                {"status": "running"},
            )
        return {
            "operation": "apply_row",
            "status": "failed" if apply_status == "failed" else "success",
            "stage": "interactive_editing",
            "summary": apply_result.get("error") or "单条应用执行完成",
            "artifacts": {
                "base_report_path": str(base_path),
                "attempt_dir": str(attempt_dir),
                "case_dir": str(case_dir),
                **self._run_store.artifacts(archive_run_dir),
            },
            "report": {"commit_count": len(affected_rows), "commits": affected_rows},
            "archive": case_result,
        }

    def preview_commit_message(
        self,
        base_report_path: str,
        row: dict[str, Any],
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
        preview_config_path = base_path
        if working_report_path:
            candidate_path = Path(working_report_path).expanduser().resolve()
            if candidate_path.exists():
                preview_config_path = candidate_path

        effective_config = preview_config_path
        sanitized_config_path: Path | None = None
        try:
            with preview_config_path.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
        except Exception:
            cfg = None
        if isinstance(cfg, dict):
            cfg.pop("target_config_layout", None)
            cfg.pop("target_config_layout_opts", None)
            layout, opts = self._normalize_layout_fields(
                target_config_layout, target_config_layout_opts
            )
            if layout and layout != "none":
                cfg["target_config_layout"] = layout
                if opts and isinstance(opts, dict):
                    cfg["target_config_layout_opts"] = opts
            sanitized_fd, sanitized_config_path_str = tempfile.mkstemp(
                suffix=".report.yml",
                prefix="preview_sanitized_",
                dir=str(preview_config_path.parent),
            )
            sanitized_config_path = Path(sanitized_config_path_str)
            with os.fdopen(sanitized_fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            effective_config = sanitized_config_path

        try:
            resolved_row = self._resolve_commit_row(
                row=row,
                base_report_path=base_report_path,
                working_report_path=working_report_path,
            )
            apply_value = self._resolve_apply_value(resolved_row)
            cmd = [
                "--action",
                "backport-batch",
                "--backport-config",
                str(effective_config),
                "--debug",
                "--json",
                "--preview-commit-message",
                "--apply",
                apply_value,
            ]
            if commit_message_template.strip():
                cmd.extend(["--commit-message-template", commit_message_template])
            commit_message_source = self._normalize_commit_message_source(
                commit_message_source
            )
            if commit_message_source != "auto":
                cmd.extend(["--commit-message-source", commit_message_source])
            if commit_message_source == "auto" and linux_repo_path.strip():
                cmd.extend(["--linux-repo-path", linux_repo_path.strip()])
            result = self._run_cvekit(
                cmd, preview_config_path.parent, operation="preview_commit_message"
            )
        finally:
            if sanitized_config_path is not None:
                sanitized_config_path.unlink(missing_ok=True)
        preview_result = self._parse_json_output(result.stdout)
        if preview_result.get("status") != "success":
            raise RuntimeError(str(preview_result.get("error") or preview_result))
        return {
            "operation": "preview_commit_message",
            "status": "success",
            "summary": "commit message 预览已生成",
            "commit_message": {
                "message": preview_result.get("commit_message")
                or preview_result.get("commit_message_preview")
                or "",
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
        patch_path = str(
            resolved_row.get(self.PATCH_KIND_TO_KEY[patch_kind]) or ""
        ).strip()
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
        target_config_layout: str = "none",
        target_config_layout_opts: dict[str, Any] | None = None,
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
        commit_message_source = BackportCvekitClient._normalize_commit_message_source(
            commit_message_source
        )
        if commit_message_source != "auto":
            config_data["commit_message_source"] = commit_message_source
        if signer_name.strip():
            config_data["signer_name"] = signer_name.strip()
        if signer_email.strip():
            config_data["signer_email"] = signer_email.strip()
        if commit_message_source == "auto" and linux_repo_path.strip():
            config_data["linux_repo_path"] = linux_repo_path.strip()
        # Strip stale layout, inject current
        config_data.pop("target_config_layout", None)
        config_data.pop("target_config_layout_opts", None)
        layout, opts = BackportCvekitClient._normalize_layout_fields(
            target_config_layout, target_config_layout_opts
        )
        if layout and layout != "none":
            config_data["target_config_layout"] = layout
            if opts and isinstance(opts, dict):
                config_data["target_config_layout_opts"] = opts
        with config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config_data, handle, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _resolve_apply_value(row: dict[str, Any]) -> str:
        for key in (
            "backported_patch_path",
            "patch_path",
            "original_patch_path",
            "commit",
            "input_commit",
        ):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        raise ValueError(f"row 中缺少可用于 apply 的字段: {list(row.keys())}")

    @staticmethod
    def _build_row_id(row: dict[str, Any]) -> str:
        for key in (
            "row_id",
            "commit",
            "input_commit",
            "original_patch_path",
            "patch_path",
            "backported_patch_path",
        ):
            val = row.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return json.dumps(row, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _normalize_layout_fields(
        target_config_layout: str,
        target_config_layout_opts: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """校验并归一化 layout 字段，非法值回落到安全默认。"""
        layout = target_config_layout
        if not isinstance(layout, str) or layout.strip() not in {"none", "anolis"}:
            layout = "none"
        else:
            layout = layout.strip()

        if layout == "none":
            return layout, None

        # 非 none layout：opts 缺失或非法时补默认值
        if isinstance(target_config_layout_opts, dict):
            try:
                from witty_service.api.backport_schemas import TargetConfigLayoutOpts

                opts: dict[str, Any] | None = TargetConfigLayoutOpts(
                    **target_config_layout_opts
                ).model_dump()
            except Exception:
                opts = {"default_level": "L1-RECOMMEND"}
        else:
            opts = {"default_level": "L1-RECOMMEND"}
        return layout, opts

    @staticmethod
    def _normalize_commit_message_source(value: str) -> str:
        return value if value in {"auto", "openEuler", "upstream"} else "upstream"

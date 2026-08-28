from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from witty_service.api.services import ServiceContainer
from witty_service.application.patchflow_paths import resolve_patchflow_state_root
from witty_service.domain.errors import DomainError

CVE_CLONE_DIR = "~/Image"
CVE_ISSUE_URL = "https://gitcode.com/src-openeuler/kernel/issues"


def _default_config() -> dict[str, str]:
    return {
        "gitcode_token": "",  # nosec B105 - 默认占位，实际值来自配置
        "signer_name": "",
        "signer_email": "",
        "clone_dir": CVE_CLONE_DIR,
        "branches": "OLK-6.6,OLK-5.10",
        "fork_repo_url": "",
        "issue_url": CVE_ISSUE_URL,
        "repo_url": "https://gitcode.com/openeuler/kernel",
    }


def _clone_dir_path(clone_dir: str) -> Path:
    return Path(clone_dir.strip()).expanduser()


class CveService:
    def __init__(self, services: ServiceContainer) -> None:
        self._services = services
        self._config_path = services.workspace_store.base_dir / "config" / "cve.json"
        self._browser_headers: dict[str, str] = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        }

    def get_config(self) -> dict[str, str]:
        if not self._config_path.exists():
            return _default_config()

        try:
            loaded = json.loads(self._config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(
                code="CVE_CONFIG_LOAD_FAILED",
                message="CVE config is invalid.",
                details={"path": str(self._config_path), "error": str(exc)},
            ) from exc

        config = _default_config()
        for key in config:
            value = loaded.get(key, "")
            config[key] = value if isinstance(value, str) else ""
        config["clone_dir"] = CVE_CLONE_DIR
        config["issue_url"] = CVE_ISSUE_URL
        return config

    def update_config(self, payload: dict[str, str]) -> None:
        config = self.get_config()
        for key in config:
            if key in {"gitcode_token", "clone_dir", "issue_url"}:
                continue
            value = payload.get(key, "")
            config[key] = value.strip() if isinstance(value, str) else ""
        config["clone_dir"] = CVE_CLONE_DIR
        config["issue_url"] = CVE_ISSUE_URL

        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def build_cvekit_mcp_runtime_config(
        self, mcp_server_config: dict[str, Any]
    ) -> dict[str, Any]:
        """生成 cvekit MCP 的运行时配置，不修改持久化配置。"""
        token = self.get_config().get("gitcode_token", "").strip()
        if not token:
            return self.sanitize_cvekit_mcp_storage_config(mcp_server_config)

        return self._inject_gitcode_token_into_cvekit_mcp_config(
            mcp_server_config, token
        )

    @staticmethod
    def sanitize_cvekit_mcp_storage_config(
        mcp_server_config: dict[str, Any],
    ) -> dict[str, Any]:
        mcp_server_config = deepcopy(mcp_server_config)
        if not isinstance(mcp_server_config, dict):
            raise DomainError(
                code="CVEKIT_MCP_CONFIG_INVALID",
                message="cvekit_mcp configuration is invalid.",
            )

        entry = mcp_server_config.get("cvekit_mcp", mcp_server_config)
        if not isinstance(entry, dict):
            raise DomainError(
                code="CVEKIT_MCP_CONFIG_INVALID",
                message="cvekit_mcp configuration is invalid.",
            )

        env = entry.get("env")
        if env is None:
            env = {}
            entry["env"] = env
        elif not isinstance(env, dict):
            raise DomainError(
                code="CVEKIT_MCP_CONFIG_INVALID",
                message="cvekit_mcp environment configuration is invalid.",
            )

        env.pop("GITCODE_TOKEN", None)
        env.pop("GITEE_TOKEN", None)
        return mcp_server_config

    @classmethod
    def _inject_gitcode_token_into_cvekit_mcp_config(
        cls, mcp_server_config: dict[str, Any], token: str
    ) -> dict[str, Any]:
        runtime_config = cls.sanitize_cvekit_mcp_storage_config(mcp_server_config)
        entry = runtime_config.get("cvekit_mcp", runtime_config)
        entry["env"]["GITCODE_TOKEN"] = token
        return runtime_config

    def update_token(self, token: str) -> None:
        token = token.strip()
        config = self.get_config()
        config["gitcode_token"] = token
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_issues(
        self, issue_url: str, limit: int = 20, *, token: str = ""
    ) -> list[dict[str, Any]]:
        org_name, repo_name = self._parse_issue_url(issue_url)
        safe_limit = max(1, min(limit or 20, 100))
        items = self._fetch_issues_from_api(
            org_name=org_name,
            repo_name=repo_name,
            limit=safe_limit,
            token=token,
        )

        filtered: list[dict[str, Any]] = []
        for item in items:
            if "CVE-" not in str(item.get("title") or "").upper():
                continue
            filtered.append(item)
            if len(filtered) >= safe_limit:
                break
        return filtered

    def search_issues(
        self,
        issue_url: str,
        query: str,
        limit: int = 20,
        *,
        token: str = "",
    ) -> list[dict[str, Any]]:
        org_name, repo_name = self._parse_issue_url(issue_url)
        safe_limit = max(1, min(limit or 20, 100))
        needle = query.strip().lower()
        if not needle:
            return self.get_issues(issue_url, safe_limit, token=token)

        items = self._fetch_issues_from_api(
            org_name=org_name,
            repo_name=repo_name,
            limit=max(safe_limit, 50),
            token=token,
            query=query.strip(),
        )

        filtered: list[dict[str, Any]] = []
        for issue in items:
            haystacks = [
                str(issue.get("number", "")),
                str(issue.get("title", "")),
                str(issue.get("body", "")),
                str(issue.get("html_url", "")),
            ]
            haystacks.extend(
                str(label.get("name", "")) for label in issue.get("labels", [])
            )
            if any(needle in value.lower() for value in haystacks if value):
                filtered.append(issue)
            if len(filtered) >= safe_limit:
                break
        return filtered

    def get_workbench(
        self, cve_id: str, branches: str, clone_dir: str = ""
    ) -> dict[str, Any]:
        branch_list = [item.strip() for item in branches.split(",") if item.strip()]
        sorted_branches = ",".join(sorted(branch_list))
        clone_root = _clone_dir_path(clone_dir or self.get_config()["clone_dir"])
        repo_dir = clone_root / "kernel"
        current_cache_key = hashlib.md5(
            f"{cve_id.strip()}|{sorted_branches}|{repo_dir}".encode(),
            usedforsecurity=False,
        ).hexdigest()
        legacy_cache_key = hashlib.md5(
            "|".join([cve_id.strip(), sorted_branches]).encode(),
            usedforsecurity=False,
        ).hexdigest()
        cache_key = current_cache_key

        home = Path.home()
        cache_path = home / ".cve_analyzer_cache" / "branches_analysis_cache.json"
        cache_items: list[dict[str, Any]] = []
        if cache_path.exists():
            try:
                cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
                for candidate_key in (current_cache_key, legacy_cache_key):
                    raw = cache_data.get(candidate_key)
                    if isinstance(raw, dict):
                        raw_items = raw.get("data", [])
                    else:
                        continue
                    if isinstance(raw_items, list):
                        cache_key = candidate_key
                        cache_items = [
                            item for item in raw_items if isinstance(item, dict)
                        ]
                        break
            except (OSError, json.JSONDecodeError):
                cache_items = []

        # CVE workbench 请求不携带 Backport task/run/artifact 标识，不能从
        # ~/.patchflow/logs/portgpt 全局目录回退挑选日志，否则会跨任务泄漏。
        # 已归档日志必须通过其所属 task/run/artifact 的路径访问；本接口没有
        # 关联记录时明确返回 missing。
        latest_log = ""

        branches_response: list[dict[str, Any]] = []
        for branch_name in branch_list:
            item = next(
                (
                    row
                    for row in cache_items
                    if str(
                        row.get("Target branch") or row.get("目标分支") or ""
                    ).strip()
                    == branch_name
                ),
                {},
            )

            status = str(
                item.get("Adaptation status")
                or item.get("适配状态")
                or item.get("Whether affected")
                or item.get("是否受影响")
                or ""
            )
            conflict_point = str(item.get("Conflict point") or item.get("冲突点") or "")
            suggested_file = str(
                item.get("Suggested adjustment files") or item.get("建议调整文件") or ""
            )
            patch_path = next(
                (
                    value
                    for value in (suggested_file, conflict_point)
                    if value and Path(value).is_file()
                ),
                conflict_point or suggested_file,
            )
            patch_file_name = Path(patch_path).name if patch_path else ""
            patch_exists = bool(patch_path and Path(patch_path).is_file())

            original_path = ""
            backport_path = ""
            backport_status = "missing"
            original_status = "missing"
            conflict_value = conflict_point.lower()

            if patch_file_name.startswith("backported_"):
                backport_path = patch_path
                backport_status = "已生成" if patch_exists else "missing"
                stem = patch_file_name[len("backported_") :]
                original_candidate = Path(patch_path).with_name(f"original_{stem}")
                if original_candidate.exists():
                    original_path = str(original_candidate)
                else:
                    original_files = sorted(
                        Path(patch_path).parent.glob("original_*.patch"),
                        key=lambda path: path.stat().st_mtime if path.exists() else 0,
                        reverse=True,
                    )
                    if original_files:
                        original_path = str(original_files[0])
                    else:
                        parts = stem.rsplit("_", 1)
                        if len(parts) == 2 and clone_dir.strip():
                            clone_candidate = (
                                _clone_dir_path(clone_dir) / f"commit_patch_{parts[1]}"
                            )
                            if clone_candidate.exists():
                                original_path = str(clone_candidate)
                original_status = (
                    "已获取"
                    if original_path and Path(original_path).exists()
                    else "missing"
                )
            elif patch_file_name.startswith(
                "commit_patch_"
            ) or conflict_value.startswith("commit"):
                original_path = patch_path
                original_status = "已获取" if patch_exists else "missing"
                backport_status = "无需回移植"
            elif patch_exists:
                original_path = patch_path
                original_status = "已获取"

            log_exists = bool(latest_log and Path(latest_log).exists())

            artifacts = [
                {
                    "kind": "original_patch",
                    "label": "原始补丁",
                    "status": original_status,
                    "path": original_path,
                    "file_name": Path(original_path).name if original_path else "",
                    "viewable": bool(original_path and Path(original_path).exists()),
                },
                {
                    "kind": "backport_patch",
                    "label": "回移植补丁",
                    "status": backport_status,
                    "path": backport_path,
                    "file_name": Path(backport_path).name if backport_path else "",
                    "viewable": bool(backport_path and Path(backport_path).exists()),
                },
                {
                    "kind": "backport_log",
                    "label": "回移植日志",
                    "status": "已获取" if log_exists else "missing",
                    "path": latest_log,
                    "file_name": Path(latest_log).name if latest_log else "",
                    "viewable": log_exists,
                },
            ]
            branches_response.append(
                {"name": branch_name, "status": status, "artifacts": artifacts}
            )

        return {
            "cve_id": cve_id.strip(),
            "cache_key": cache_key,
            "branches": branches_response,
        }

    def get_pr_readiness(
        self,
        cve_id: str,
        branches: str,
        clone_dir: str = "",
        issue_number: str = "",
    ) -> dict[str, Any]:
        config = self.get_config()
        clone_root = _clone_dir_path(clone_dir or config["clone_dir"])
        repo_dir = clone_root / "kernel"
        path_hash = hashlib.md5(
            str(repo_dir).encode(), usedforsecurity=False
        ).hexdigest()[:6]
        branch_list = [item.strip() for item in branches.split(",") if item.strip()]
        fix_branch_suffixes = [
            item
            for item in [
                issue_number.strip(),
                cve_id.strip(),
                cve_id.strip().rsplit("-", 1)[-1],
            ]
            if item
        ]

        branches_response: list[dict[str, Any]] = []
        for branch_name in branch_list:
            fix_branch = (
                f"fix-{branch_name}-{fix_branch_suffixes[0]}"
                if fix_branch_suffixes
                else f"fix-{branch_name}"
            )
            ready = False
            reason = "fix_branch_missing"

            if not repo_dir.exists():
                reason = "clone_dir_missing"
            elif not (repo_dir / ".git").exists():
                reason = "not_git_repo"
            else:
                result = subprocess.run(
                    ["git", "-C", str(repo_dir), "branch", "-v"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                local_branches = {
                    line.strip().lstrip("*").strip().split(maxsplit=1)[0]
                    for line in result.stdout.splitlines()
                    if line.strip()
                }
                for suffix in fix_branch_suffixes:
                    for candidate in (
                        f"fix-{branch_name}-{suffix}-{path_hash}",
                        f"fix-{branch_name}-{suffix}",
                    ):
                        if candidate in local_branches:
                            ready = True
                            fix_branch = candidate
                            break
                    if ready:
                        break
                reason = "local_fix_branch_exists" if ready else "fix_branch_missing"

            branches_response.append(
                {
                    "branch": branch_name,
                    "fix_branch": fix_branch,
                    "ready": ready,
                    "reason": reason,
                }
            )

        return {
            "cve_id": cve_id.strip(),
            "ready": any(item["ready"] for item in branches_response),
            "branches": branches_response,
        }

    def read_artifact(self, raw_path: str) -> dict[str, str]:
        artifact_path = Path(raw_path).expanduser().resolve()
        home = Path.home().resolve()
        config = self.get_config()
        allowed_roots = [
            home / ".cve_analyzer_cache",
            resolve_patchflow_state_root() / "logs",
            home / "backports" / "patch_dataset",
        ]
        allowed = False
        for root in allowed_roots:
            try:
                artifact_path.relative_to(root.resolve())
                allowed = True
                break
            except ValueError:
                continue
        clone_dir = config.get("clone_dir", "").strip()
        if not allowed and clone_dir and artifact_path.name.startswith("commit_patch_"):
            try:
                artifact_path.relative_to(Path(clone_dir).expanduser().resolve())
                allowed = True
            except ValueError:
                pass

        if not allowed or not artifact_path.is_file():
            raise DomainError(
                code="CVE_ARTIFACT_NOT_FOUND",
                message="CVE artifact is not readable.",
                details={"path": raw_path},
            )

        return {
            "path": str(artifact_path),
            "file_name": artifact_path.name,
            "content": artifact_path.read_text(encoding="utf-8", errors="replace"),
        }

    def _parse_issue_url(self, issue_url: str) -> tuple[str, str]:
        parsed = urlparse(issue_url.strip())
        path = parsed.path.strip("/")
        if path.endswith("/issues"):
            path = path[: -len("/issues")]
        if path.endswith(".git"):
            path = path[:-4]

        parts = [part for part in path.split("/") if part]
        if len(parts) < 2:
            raise DomainError(
                code="INVALID_CVE_ISSUE_URL",
                message="Invalid CVE issue URL.",
                details={"issue_url": issue_url},
            )
        return parts[0], parts[1]

    def _fetch_issues_from_api(
        self,
        *,
        org_name: str,
        repo_name: str,
        limit: int,
        token: str,
        query: str = "",
    ) -> list[dict[str, Any]]:
        endpoints = [
            "https://api.atomgit.com/api/v5",
            "https://gitcode.com/api/v5",
        ]
        errors: list[dict[str, Any]] = []

        for base in endpoints:
            url = self._build_issue_api_url(
                base=base,
                org_name=org_name,
                repo_name=repo_name,
                limit=limit,
                query=query,
            )
            headers = dict(self._browser_headers)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                    response = client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                errors.append({"base": base, "error": str(exc)})
                continue

            if response.status_code >= 400:
                errors.append(
                    {
                        "base": base,
                        "status_code": response.status_code,
                        "response_text": response.text[:500],
                    }
                )
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                errors.append({"base": base, "error": str(exc)})
                continue

            if not isinstance(payload, list):
                errors.append({"base": base, "error": "payload is not a list"})
                continue

            return [
                self._normalize_issue(issue, org_name, repo_name) for issue in payload
            ]

        if errors and all(
            self._is_invalid_gitcode_token_error(error) for error in errors
        ):
            raise DomainError(
                code="CVE_GITCODE_TOKEN_INVALID",
                message="GitCode token is invalid.",
                details={
                    "org_name": org_name,
                    "repo_name": repo_name,
                    "errors": errors,
                },
            )

        raise DomainError(
            code="CVE_ISSUES_FETCH_FAILED",
            message="Failed to fetch CVE issues.",
            details={"org_name": org_name, "repo_name": repo_name, "errors": errors},
        )

    def _build_issue_api_url(
        self,
        *,
        base: str,
        org_name: str,
        repo_name: str,
        limit: int,
        query: str = "",
    ) -> str:
        url = (
            f"{base}/repos/{org_name}/{repo_name}/issues"
            f"?state=all&sort=updated&direction=desc&page=1&per_page={limit}"
        )
        if query:
            url += f"&search={query}"
        return url

    def _normalize_issue(
        self, issue: dict[str, Any], org_name: str, repo_name: str
    ) -> dict[str, Any]:
        number = int(issue.get("number") or issue.get("iid") or 0)
        html_url = (
            issue.get("html_url")
            or f"https://gitcode.com/{org_name}/{repo_name}/issues/{number}"
        )

        labels: list[dict[str, str]] = []
        raw_labels = issue.get("labels") or []
        if isinstance(raw_labels, list):
            for label in raw_labels:
                if isinstance(label, dict):
                    labels.append(
                        {
                            "name": str(label.get("name") or ""),
                            "color": str(label.get("color") or ""),
                        }
                    )
                else:
                    labels.append({"name": str(label), "color": ""})

        raw_user = issue.get("user") or {}
        if not isinstance(raw_user, dict):
            raw_user = {}

        return {
            "id": int(issue.get("id") or number),
            "number": number,
            "title": str(issue.get("title") or ""),
            "body": str(issue.get("body") or ""),
            "state": str(issue.get("state") or ""),
            "html_url": str(html_url),
            "created_at": str(issue.get("created_at") or ""),
            "updated_at": str(issue.get("updated_at") or ""),
            "labels": labels,
            "user": {
                "login": str(raw_user.get("login") or ""),
                "avatar_url": str(raw_user.get("avatar_url") or ""),
            },
        }

    @staticmethod
    def _is_invalid_gitcode_token_error(error: dict[str, Any]) -> bool:
        response_text = str(error.get("response_text") or "").lower()
        return error.get("status_code") == 404 and "token not found" in response_text

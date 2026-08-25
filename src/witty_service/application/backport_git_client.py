from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any


class BackportGitClient:
    """Git 操作封装，直接 subprocess → git，不经过中间脚本。"""

    @staticmethod
    def _run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    @staticmethod
    def ensure_git_repo(path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"target_path 不存在: {path}")
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise NotADirectoryError(f"target_path 不是 git 仓库: {path}")

    @staticmethod
    def remote_url(path: Path) -> str:
        result = BackportGitClient._run_git(path, ["remote", "get-url", "origin"])
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def current_branch(path: Path) -> str:
        result = BackportGitClient._run_git(path, ["branch", "--show-current"])
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def head(path: Path) -> str:
        result = BackportGitClient._run_git(path, ["rev-parse", "HEAD"])
        if result.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD 失败: {result.stderr.strip()}")
        return result.stdout.strip()

    @staticmethod
    def resolve_ref(target_path: str, ref: str = "HEAD") -> str:
        repo = Path(target_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo)
        requested_ref = ref.strip() or "HEAD"
        result = BackportGitClient._run_git(
            repo,
            ["rev-parse", "--verify", f"{requested_ref}^{{commit}}"],
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"git 无法解析目标 ref {requested_ref}: {detail}")
        return result.stdout.strip()

    @staticmethod
    def branches(path: Path) -> tuple[list[str], list[str]]:
        local_result = BackportGitClient._run_git(path, ["branch", "--format=%(refname:short)"])
        remote_result = BackportGitClient._run_git(path, ["branch", "-r", "--format=%(refname:short)"])
        local_branches = [
            line.strip()
            for line in local_result.stdout.splitlines()
            if line.strip() and not line.strip().startswith("(")
        ] if local_result.returncode == 0 else []
        remote_branches = [
            line.strip()
            for line in remote_result.stdout.splitlines()
            if line.strip() and not line.strip().endswith("/HEAD")
        ] if remote_result.returncode == 0 else []
        return local_branches, remote_branches

    @staticmethod
    def default_branch(path: Path) -> str:
        result = BackportGitClient._run_git(path, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
        if result.returncode == 0:
            return result.stdout.strip().removeprefix("origin/")
        return BackportGitClient.current_branch(path)

    @staticmethod
    def get_repo_state(target_path: str) -> dict[str, Any]:
        repo = Path(target_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo)

        head_result = BackportGitClient._run_git(repo, ["rev-parse", "HEAD"])
        if head_result.returncode != 0:
            raise RuntimeError(f"git rev-parse HEAD 失败: {head_result.stderr.strip()}")

        branch_result = BackportGitClient._run_git(repo, ["branch", "--show-current"])
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""

        status_result = BackportGitClient._run_git(repo, ["status", "--porcelain=v1", "-uall"])
        if status_result.returncode != 0:
            raise RuntimeError(f"git status 失败: {status_result.stderr.strip()}")

        return {
            "target_path": str(repo),
            "target_branch": branch,
            "target_head": head_result.stdout.strip(),
            "target_status_clean": status_result.stdout.strip() == "",
        }

    @staticmethod
    def list_commits_between(target_path: str, old_head: str, new_head: str) -> list[dict[str, str]]:
        if not old_head.strip() or not new_head.strip() or old_head.strip() == new_head.strip():
            return []
        repo = Path(target_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo)
        result = BackportGitClient._run_git(
            repo,
            ["log", "--reverse", "--pretty=format:%H%x1f%s%x1e", f"{old_head.strip()}..{new_head.strip()}"],
        )
        if result.returncode != 0:
            return []

        entries: list[dict[str, str]] = []
        for chunk in result.stdout.split("\x1e"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split("\x1f", 1)
            if len(parts) != 2:
                continue
            entries.append({"hash": parts[0].strip(), "subject": parts[1].strip()})
        return entries

    @staticmethod
    def load_git_log(target_path: str, limit: int = 100) -> list[dict[str, str]]:
        path_str = target_path.strip()
        if not path_str:
            return []

        repo = Path(path_str).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo)

        cmd = [
            "git",
            "-C",
            str(repo),
            "log",
            "--decorate",
            "--date=iso-strict",
            "-n",
            str(limit),
            "--pretty=format:%H%x1f%h%x1f%d%x1f%s%x1f%cI%x1e",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            return []
        entries: list[dict[str, str]] = []
        for chunk in result.stdout.split("\x1e"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split("\x1f")
            if len(parts) != 5:
                continue
            entries.append(
                {
                    "hash": parts[0],
                    "shortHash": parts[1],
                    "refs": parts[2],
                    "subject": parts[3],
                    "committedAt": parts[4],
                }
            )
        return entries

    @staticmethod
    def load_git_show(target_path: str, revision: str) -> str:
        repo = Path(target_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo)

        cmd = [
            "git",
            "-C",
            str(repo),
            "show",
            "--stat",
            "--decorate",
            "--no-color",
            revision,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git show 失败: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    @staticmethod
    def check_manual_patch(target_path: str, patch_text: str) -> dict[str, str]:
        repo = Path(target_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo)
        if not patch_text.strip():
            raise ValueError("patch_text 不能为空")

        with tempfile.NamedTemporaryFile("w", suffix=".patch", encoding="utf-8", delete=True) as patch_file:
            patch_file.write(patch_text)
            patch_file.flush()
            result = subprocess.run(
                ["git", "-C", str(repo), "apply", "--check", patch_file.name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        return {
            "returncode": str(result.returncode),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }

    @staticmethod
    def apply_manual_patch(target_path: str, patch_text: str) -> dict[str, str]:
        repo = Path(target_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo)
        if not patch_text.strip():
            raise ValueError("patch_text 不能为空")

        with tempfile.NamedTemporaryFile("w", suffix=".patch", encoding="utf-8", delete=True) as patch_file:
            patch_file.write(patch_text)
            patch_file.flush()
            result = subprocess.run(
                ["git", "-C", str(repo), "apply", patch_file.name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        return {
            "returncode": str(result.returncode),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }

    @staticmethod
    def check_patch_file(target_path: str, patch_path: str, *, reverse: bool = False) -> dict[str, str]:
        repo = Path(target_path).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo)
        patch = Path(patch_path).expanduser().resolve()
        if not patch.exists():
            raise FileNotFoundError(f"patch 文件不存在: {patch}")

        cmd = ["git", "-C", str(repo), "apply", "--check"]
        if reverse:
            cmd.append("--reverse")
        cmd.append(str(patch))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return {
            "returncode": str(result.returncode),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }

    @staticmethod
    def collect_subject_map(target_path: str, limit: int = 200) -> dict[str, str]:
        path_str = target_path.strip()
        if not path_str:
            return {}
        repo = Path(path_str).expanduser().resolve()
        BackportGitClient.ensure_git_repo(repo)

        cmd = [
            "git",
            "-C",
            str(repo),
            "log",
            "-n",
            str(limit),
            "--date=iso-strict",
            "--pretty=format:%H%x1f%s",
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            return {}

        subject_map: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\x1f", 1)
            if len(parts) != 2:
                continue
            commit_hash, subject = parts
            normalized = subject.strip()
            if normalized and normalized not in subject_map:
                subject_map[normalized] = commit_hash.strip()
        return subject_map

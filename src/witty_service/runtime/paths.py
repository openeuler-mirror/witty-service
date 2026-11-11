from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from witty_service.config import get_settings

RUNTIME_HOME_DIR = "runtime"

_PASSTHROUGH_ENV_KEYS = (
    "PATH",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TMP",
    "TEMP",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def workspace_root() -> Path:
    return get_settings().workspace.root_path().resolve()


def validate_workspace_id(workspace_id: str) -> None:
    if not workspace_id or workspace_id in {".", ".."}:
        raise ValueError(f"Invalid workspace_id: {workspace_id!r}")
    if "/" in workspace_id or "\\" in workspace_id or "\x00" in workspace_id:
        raise ValueError(f"Invalid workspace_id: {workspace_id!r}")
    if PurePosixPath(workspace_id).parts != (workspace_id,):
        raise ValueError(f"Invalid workspace_id: {workspace_id!r}")


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimePaths:
    workspace_id: str
    root: Path
    runtime_home: Path
    config: Path
    data: Path
    state: Path
    cache: Path

    @classmethod
    def for_workspace(
        cls, workspace_id: str, *, root: Path | str | None = None
    ) -> WorkspaceRuntimePaths:
        validate_workspace_id(workspace_id)
        base = (
            Path(root).expanduser().resolve() if root is not None else workspace_root()
        )
        runtime_home = base / RUNTIME_HOME_DIR / workspace_id
        return cls(
            workspace_id=workspace_id,
            root=base,
            runtime_home=runtime_home,
            config=runtime_home / "config",
            data=runtime_home / "data",
            state=runtime_home / "state",
            cache=runtime_home / "cache",
        )

    def ensure(self) -> None:
        for path in (
            self.root / RUNTIME_HOME_DIR,
            self.runtime_home,
            self.config,
            self.data,
            self.state,
            self.cache,
        ):
            if path.is_symlink():
                raise ValueError(f"Runtime home must not be a symlink: {path}")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """构造与 passwd home 隔离的 OpenCode 运行环境。

        只透传与网络/语言相关的基础变量：``OPENCODE_*`` 与调用方私有变量一律
        丢弃，避免 ``OPENCODE_CONFIG``/``OPENCODE_DB`` 等改写配置源与数据目录。
        """
        source = os.environ if base is None else base
        env = {key: source[key] for key in _PASSTHROUGH_ENV_KEYS if key in source}
        env.update(
            {
                "HOME": str(self.runtime_home),
                "XDG_CONFIG_HOME": str(self.config),
                "XDG_DATA_HOME": str(self.data),
                "XDG_STATE_HOME": str(self.state),
                "XDG_CACHE_HOME": str(self.cache),
            }
        )
        return env


def resolve_workspace_path(workspace_directory: str | Path, relative_path: str) -> Path:
    """把用户可见的相对路径解析到 workspace 内，拒绝穿越与符号链接逃逸。"""
    root = Path(workspace_directory).expanduser().resolve()
    normalized = _relative_parts(relative_path)
    candidate = (root / normalized).resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Path escapes workspace: {relative_path!r}")
    return candidate


def workspace_relative_path(workspace_directory: str | Path, path: str | Path) -> str:
    """把 workspace 内路径转换为稳定的 POSIX 相对路径。"""
    root = Path(workspace_directory).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = resolve_workspace_path(root, candidate.as_posix())
    else:
        candidate = candidate.resolve(strict=False)
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"Path is outside workspace: {path!s}")
    return candidate.relative_to(root).as_posix()


def _relative_parts(relative_path: str) -> PurePosixPath:
    if not relative_path or relative_path in {".", ".."}:
        raise ValueError(f"Invalid relative path: {relative_path!r}")
    if "\x00" in relative_path or "\\" in relative_path:
        raise ValueError(f"Invalid relative path: {relative_path!r}")
    posix = PurePosixPath(relative_path)
    if (
        posix.is_absolute()
        or PureWindowsPath(relative_path).is_absolute()
        or PureWindowsPath(relative_path).drive
    ):
        raise ValueError(f"Invalid relative path: {relative_path!r}")
    if any(part == ".." for part in posix.parts):
        raise ValueError(f"Invalid relative path: {relative_path!r}")
    return posix

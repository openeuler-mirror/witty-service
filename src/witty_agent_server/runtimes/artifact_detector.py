"""产物识别与 ``artifact.*`` 事件构建（ADR-0001 D2/D3）。

``write`` 工具写出的文件扩展名命中白名单时产出产物事件（当前先不约束目录，
交付物写在任意路径下都能出卡片）。路径不允许空值与 ``..`` 穿越；绝对路径原样
保留，交由 witty-service 边界基于 ``agent.workspace_path`` 做真实
``relative_to`` 归一化，越界事件在边界丢弃（见 ``witty_service.application.artifact_paths``）。
敏感文件（凭据/密钥类）命中 denylist 时不内联 ``content``。事件携带
``{id, name, type, status, version, relative_path, size, mime, content?}``，
文本产物内联 ``content`` 上限 512KB，超限/二进制只带 ``relative_path``。
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

# 常见代码/脚本/配置类扩展名（均为文本产物，命中后类型为 code、可内联 content）
_CODE_MIME_BY_EXTENSION: dict[str, str] = {
    ".py": "text/x-python",
    ".ts": "text/typescript",
    ".tsx": "text/typescript-tsx",
    ".jsx": "text/jsx",
    ".go": "text/x-go",
    ".rs": "text/rust",
    ".java": "text/x-java-source",
    ".c": "text/x-c",
    ".h": "text/x-c-header",
    ".cc": "text/x-c++",
    ".cpp": "text/x-c++",
    ".hpp": "text/x-c++-header",
    ".cxx": "text/x-c++",
    ".cs": "text/x-csharp",
    ".rb": "text/x-ruby",
    ".php": "text/x-php",
    ".swift": "text/x-swift",
    ".kt": "text/x-kotlin",
    ".kts": "text/x-kotlin",
    ".scala": "text/x-scala",
    ".sh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".zsh": "text/x-shellscript",
    ".sql": "text/x-sql",
    ".vue": "text/x-vue",
    ".svelte": "text/x-svelte",
    ".lua": "text/x-lua",
    ".dart": "text/x-dart",
    ".jl": "text/x-julia",
    ".pl": "text/x-perl",
    ".ex": "text/x-elixir",
    ".exs": "text/x-elixir",
    ".hs": "text/x-haskell",
    ".clj": "text/x-clojure",
    ".groovy": "text/x-groovy",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".xml": "application/xml",
}

@dataclass(frozen=True)
class _ArtifactSpec:
    """单条产物扩展名规格：产物类型、MIME、是否可内联 ``content``。"""

    type: str
    mime: str
    inlineable: bool


# 富产物（图片/视频/文档等，非代码）规格：扩展名 -> (类型, MIME, 可内联)
_RICH_ARTIFACT_BY_EXTENSION: dict[str, tuple[str, str, bool]] = {
    ".html": ("html", "text/html", True),
    ".png": ("image", "image/png", False),
    ".jpg": ("image", "image/jpeg", False),
    ".jpeg": ("image", "image/jpeg", False),
    ".gif": ("image", "image/gif", False),
    ".webp": ("image", "image/webp", False),
    ".mp4": ("video", "video/mp4", False),
    ".webm": ("video", "video/webm", False),
    ".md": ("markdown", "text/markdown", True),
    ".js": ("code", "text/javascript", True),
    ".css": ("code", "text/css", True),
    ".json": ("code", "application/json", True),
    ".csv": ("code", "text/csv", True),
    ".pdf": ("pdf", "application/pdf", False),
}

# 单一事实源：扩展名 -> 规格。ARTIFACT_EXTENSIONS / INLINE_ARTIFACT_EXTENSIONS
# 及 detect_artifact 的 type/mime 判定全部从此表派生，避免多处维护产生漂移。
_ARTIFACT_SPEC: dict[str, _ArtifactSpec] = {
    **{
        ext: _ArtifactSpec(type="code", mime=mime, inlineable=True)
        for ext, mime in _CODE_MIME_BY_EXTENSION.items()
    },
    **{
        ext: _ArtifactSpec(type=type_, mime=mime, inlineable=inlineable)
        for ext, (type_, mime, inlineable) in _RICH_ARTIFACT_BY_EXTENSION.items()
    },
}

ARTIFACT_EXTENSIONS = frozenset(_ARTIFACT_SPEC)
INLINE_ARTIFACT_EXTENSIONS = frozenset(
    ext for ext, spec in _ARTIFACT_SPEC.items() if spec.inlineable
)

INLINE_CONTENT_LIMIT = 512 * 1024

# 敏感文件 denylist：按文件名匹配，命中后不内联 content（事件仍保留路径）。
SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    # 环境变量 / 凭据 / 配置类
    ".env",
    ".env.*",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".dockerconfigjson",
    "kubeconfig",
    "*.kubeconfig",
    # 私钥 / 证书 / 密钥库
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa",
    "id_ed25519",
    "id_ed25519_sk",
    "id_ecdsa",
    "id_dsa",
    # 名称语义（凭据 / 密钥）
    "*credential*",
    "*credentials*",
    "*secret*",
    "*secrets*",
    "*.secret",
)

def normalize_relative_path(path_value: Any) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = path_value.strip().replace("\\", "/")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    # 绝对路径原样保留（含前导 "/"），交由 witty-service 边界做 relative_to 归一化；
    # 此处只负责路径形态卫生（无空路径、无 .. 穿越），不承担包含校验（跨进程信任假设
    # 已通过边界单点校验消除，见 artifact_paths.resolve_within_workspace）。
    return "/" + "/".join(parts) if path.startswith("/") else "/".join(parts)


def is_sensitive_file(path_value: Any) -> bool:
    """按文件名判定是否属于敏感文件（命中则不内联 content）。"""
    if not isinstance(path_value, str):
        return False
    name = PurePosixPath(path_value).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_FILE_PATTERNS)


def detect_artifact(path_value: Any) -> dict[str, Any] | None:
    relative_path = normalize_relative_path(path_value)
    if relative_path is None:
        return None
    extension = PurePosixPath(relative_path).suffix.lower()
    spec = _ARTIFACT_SPEC.get(extension)
    if spec is None:
        return None
    return {
        "id": relative_path,
        "name": PurePosixPath(relative_path).name,
        "type": spec.type,
        "version": 1,
        "relative_path": relative_path,
        "mime": spec.mime,
    }


def extract_write_arguments(args: Any) -> dict[str, Any]:
    if isinstance(args, str) and args:
        try:
            parsed = json.loads(args)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return args if isinstance(args, dict) else {}


def pick_write_path(args: dict[str, Any]) -> Any:
    for key in ("file_path", "filePath", "path", "file", "relative_path"):
        if key in args:
            return args[key]
    return None


def pick_write_content(args: dict[str, Any]) -> Any:
    for key in ("content", "text", "data"):
        if key in args:
            return args[key]
    return None


def build_artifact_event(
    *,
    args: Any,
    status: str,
    inline: bool = True,
) -> dict[str, Any] | None:
    raw = extract_write_arguments(args)
    meta = detect_artifact(pick_write_path(raw))
    if meta is None:
        return None
    content = pick_write_content(raw)
    size: int | None = None
    if isinstance(content, str):
        size = len(content.encode("utf-8"))
    payload: dict[str, Any] = {
        "id": meta["id"],
        "name": meta["name"],
        "type": meta["type"],
        "status": status,
        "version": meta["version"],
        "relative_path": meta["relative_path"],
        "size": size,
        "mime": meta["mime"],
    }
    if (
        inline
        and status == "ready"
        and isinstance(content, str)
        and PurePosixPath(meta["relative_path"]).suffix.lower()
        in INLINE_ARTIFACT_EXTENSIONS
        and size is not None
        and size <= INLINE_CONTENT_LIMIT
        and not is_sensitive_file(meta["relative_path"])
    ):
        payload["content"] = content
    return payload


def artifact_started_event(args: Any) -> dict[str, Any] | None:
    return build_artifact_event(args=args, status="creating", inline=False)


def artifact_completed_event(
    args: Any, *, is_error: bool = False
) -> dict[str, Any] | None:
    return build_artifact_event(
        args=args,
        status="error" if is_error else "ready",
        inline=not is_error,
    )

from __future__ import annotations

from pathlib import Path


_DEFAULT_OPENCLAW_HOME = Path.home() / ".openclaw"


def resolve_openclaw_home_dir(*, profile_name: str | None = None) -> Path:
    """按 profile 解析 OpenClaw home 根目录。"""
    if isinstance(profile_name, str) and profile_name:
        return Path.home() / f".openclaw-{profile_name}"
    return _DEFAULT_OPENCLAW_HOME


def resolve_openclaw_output_path(*, profile_name: str | None = None) -> Path:
    """按 profile 解析 OpenClaw 配置文件路径。"""
    return resolve_openclaw_home_dir(profile_name=profile_name) / "openclaw.json"

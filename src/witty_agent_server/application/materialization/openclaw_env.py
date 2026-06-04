from __future__ import annotations

import os
from pathlib import Path


def build_openclaw_env(
    *, state_dir: str | Path, base_env: dict[str, str] | None = None
) -> dict[str, str]:
    """构造 OpenClaw 子进程环境，并保留父进程 PATH 等必要变量。"""
    env = dict(base_env or os.environ)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    return env

"""验证 Runtime Home 的配置源隔离。

对照：``HOME`` 指向带 ``.config/opencode/opencode.json`` 哨兵的假 passwd home，
并显式设置 ``OPENCODE_CONFIG``，确认哨兵会被加载。

隔离：同一假 passwd home，用 ``build_local_opencode_spec`` 经真实 Manager 启动
OpenCode；只应加载 Runtime Home 下的配置，哨兵与 ``OPENCODE_CONFIG`` 都不生效。

运行：

    WITTY_PROBE_OPENCODE_DIR=/opt/witty-service/opencode-1.17.13 \
      .venv/bin/python -m pytest tests/probes/test_runtime_home_isolation.py \
      -m probe -q
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.probes.probe_env import (
    free_port,
    probe_tmp_root,
    resolve_opencode_bin,
    verify_opencode_binary,
)
from witty_service.bootstrap import (
    build_local_opencode_spec,
    build_workspace_runtime_manager,
)
from witty_service.runtime.contracts import RuntimeWorkspace
from witty_service.runtime.paths import WorkspaceRuntimePaths
from witty_service.sandbox.local_process import LocalProcessSandboxBackend

pytestmark = pytest.mark.probe


def _write_config(directory: Path, agent: str) -> None:
    target = directory / "opencode" / "opencode.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"agent": {agent: {"description": agent, "mode": "primary"}}}),
        encoding="utf-8",
    )


def _base_env(home: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("XDG_")
    }
    env["HOME"] = str(home)
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    return env


@contextlib.contextmanager
def _serve(bin_path: Path, env: dict[str, str]) -> Iterator[str]:
    port = free_port()
    process = subprocess.Popen(
        [str(bin_path), "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            if process.poll() is not None:
                pytest.fail(f"opencode serve exited rc={process.returncode}")
            try:
                url = f"http://127.0.0.1:{port}"
                if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                    yield url
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        pytest.fail("opencode serve did not become healthy")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)


def _loaded_agents(url: str) -> set[str]:
    response = httpx.get(f"{url}/config", timeout=10)
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return set(payload.get("agent") or {})


def test_runtime_home_isolates_passwd_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_path = resolve_opencode_bin()
    if bin_path is None:
        pytest.fail("未找到 OpenCode 1.17.13 可执行文件", pytrace=False)
    verify_opencode_binary(bin_path)
    root = probe_tmp_root() / f"runtime-home-{uuid.uuid4().hex[:12]}"
    home = root / "passwd-home"
    runtime = WorkspaceRuntimePaths.for_workspace("ws_iso", root=root)
    try:
        _write_config(home / ".config", "passwd-agent")
        _write_config(runtime.config, "runtime-agent")
        runtime.ensure()
        sentinel = str(home / ".config/opencode/opencode.json")
        monkeypatch.setenv("OPENCODE_CONFIG", sentinel)

        with _serve(bin_path, _base_env(home)) as url:
            assert "passwd-agent" in _loaded_agents(url)

        asyncio.run(_assert_manager_isolation(bin_path, root))
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def _assert_manager_isolation(bin_path: Path, root: Path) -> None:
    workspace_dir = root / "workspace"
    workspace_dir.mkdir()
    workspace = RuntimeWorkspace(
        workspace_id="ws_iso", runtime_directory=str(workspace_dir)
    )
    spec = build_local_opencode_spec(
        workspace=workspace,
        executable=str(bin_path),
        runtime_root=str(root),
    )
    runtime_home = root / "runtime" / "ws_iso"
    assert spec.env["HOME"] == str(runtime_home)
    assert spec.env["XDG_CONFIG_HOME"] == str(runtime_home / "config")
    assert "OPENCODE_CONFIG" not in spec.env

    manager = build_workspace_runtime_manager(sandbox=LocalProcessSandboxBackend())
    try:
        state = await manager.ensure_started(spec)
        assert state.base_url is not None
        loaded = _loaded_agents(state.base_url)
        assert "runtime-agent" in loaded
        assert "passwd-agent" not in loaded
        assert (runtime_home / "opencode-serve.log").is_file()
    finally:
        await manager.shutdown()

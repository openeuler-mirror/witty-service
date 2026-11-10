"""OpenCode 1.17.13 探针运行环境。

职责：

1. 解析并强制校验探针使用的 OpenCode 可执行文件版本与 npm 包身份
   （``opencode-ai``），不匹配直接失败，不 skip。
2. 在独立目录中构造隔离的 XDG config/data/state/cache，绝不污染开发机上的
   ``~/.config/opencode`` 和 ``~/.local/share/opencode``。
3. 写入探针专用 ``opencode.json``：mock provider、本地 stdio MCP、Skill 目录、
   两个探针 Agent。
4. 启停 ``opencode serve`` 子进程（独立进程组），等待 health 与 MCP ready，
   并提供会话、prompt、消息、事件的最小封装。
5. 事件流 ``EventCollector`` 解析 ``id:`` / ``event:`` / ``data:``，支持
   ``directory`` 作用域与 ``after`` 透传；异常不再被静默吞掉。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

PROBE_OPENCODE_VERSION = "1.17.13"
PROBE_PACKAGE_NAME = "opencode-ai"
_PROBE_DIR_ENV = "WITTY_PROBE_OPENCODE_DIR"
_TMP_ROOT_ENV = "WITTY_PROBE_TMP_ROOT"
_KEEP_TMP_ENV = "WITTY_PROBE_KEEP_TMP"
_READY_TIMEOUT = 40.0
_PROMPT_TIMEOUT = 60.0
_MCP_TIMEOUT = 30.0

SKILL_NAME = "probe-skill"
SKILL_BODY = "PROBE_SKILL_BODY_7f3a"
AGENT_A_PROMPT = "PROBE_AGENT_A_SYSTEM_PROMPT"
AGENT_B_PROMPT = "PROBE_AGENT_B_SYSTEM_PROMPT"


class ProbeEnvironmentError(RuntimeError):
    """探针环境无法满足固定版本/包身份等前提。"""


@dataclass(frozen=True)
class BinaryInfo:
    path: Path
    version: str
    package_name: str
    package_version: str


def probe_tmp_root() -> Path:
    """探针临时目录根。

    默认放到用户 cache，而不是 ``/tmp``：tmpfs 很容易被 OpenCode 数据写满，
    写满后 shell 与 pytest 都会以难以诊断的方式失败。
    """
    override = os.environ.get(_TMP_ROOT_ENV)
    if override:
        return Path(override)
    return Path.home() / ".cache" / "witty-probe"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def resolve_opencode_bin() -> Path | None:
    """返回探针要用的 ``opencode`` 可执行文件；找不到返回 ``None``。

    优先级：``WITTY_PROBE_OPENCODE_BIN`` → ``WITTY_PROBE_OPENCODE_DIR`` 下的
    npm 安装布局 → ``PATH``。版本与包身份由 :func:`verify_opencode_binary`
    负责，此处不替调用方兜底。
    """
    override = os.environ.get("WITTY_PROBE_OPENCODE_BIN")
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    probe_dir = os.environ.get(_PROBE_DIR_ENV)
    if probe_dir:
        base = Path(probe_dir)
        for candidate in (
            base / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
            base / "bin" / "opencode.exe",
            base / "bin" / "opencode",
        ):
            if candidate.is_file():
                return candidate
        return None
    for name in ("opencode.exe", "opencode"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _find_package_json(bin_path: Path) -> Path | None:
    for parent in list(bin_path.resolve().parents)[:5]:
        candidate = parent / "package.json"
        if candidate.is_file():
            return candidate
    return None


def verify_opencode_binary(bin_path: Path) -> BinaryInfo:
    """校验 ``--version`` 与 npm 包身份；不满足时抛出显式错误。"""
    if not bin_path.is_file():
        raise ProbeEnvironmentError(f"OpenCode 可执行文件不存在: {bin_path}")
    env = {**os.environ, "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    try:
        result = subprocess.run(
            [str(bin_path), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )
    except OSError as exc:
        raise ProbeEnvironmentError(f"无法执行 {bin_path}: {exc}") from exc
    if result.returncode != 0:
        raise ProbeEnvironmentError(
            f"{bin_path} --version 失败 rc={result.returncode}: "
            f"{result.stdout.strip()} {result.stderr.strip()}"
        )
    version = result.stdout.strip().splitlines()[-1].strip() if result.stdout else ""
    if version != PROBE_OPENCODE_VERSION:
        raise ProbeEnvironmentError(
            f"OpenCode 版本必须是 {PROBE_OPENCODE_VERSION}，实际是 {version!r}；"
            "请安装固定版本或设置 WITTY_PROBE_OPENCODE_BIN"
        )
    package_json = _find_package_json(bin_path)
    if package_json is None:
        raise ProbeEnvironmentError(
            f"无法从 {bin_path} 向上找到 package.json，不能验证包身份；"
            "请使用 npm 布局并通过 WITTY_PROBE_OPENCODE_DIR / "
            "WITTY_PROBE_OPENCODE_BIN 指向该布局内的可执行文件"
        )
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeEnvironmentError(f"无法读取 {package_json}: {exc}") from exc
    package_name = str(package.get("name", ""))
    package_version = str(package.get("version", ""))
    if package_name != PROBE_PACKAGE_NAME:
        raise ProbeEnvironmentError(
            f"包身份必须是 {PROBE_PACKAGE_NAME}，实际是 {package_name!r} "
            f"({package_json})"
        )
    if package_version != PROBE_OPENCODE_VERSION:
        raise ProbeEnvironmentError(
            f"{package_json} 的版本是 {package_version!r}，"
            f"与 --version {version!r} 不一致"
        )
    return BinaryInfo(
        path=bin_path,
        version=version,
        package_name=package_name,
        package_version=package_version,
    )


def _write_skill(skill_dir: Path) -> None:
    target = skill_dir / SKILL_NAME
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        "---\n"
        f"name: {SKILL_NAME}\n"
        "description: Probe skill used to verify global skill exposure\n"
        "---\n\n"
        f"# {SKILL_NAME}\n\n{SKILL_BODY}\n",
        encoding="utf-8",
    )


def _scan_run_processes(run_id: str) -> list[int]:
    """按唯一 run id 扫描 /proc，找出 MCP 等残留子进程。"""
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        if run_id in raw.decode("utf-8", "replace"):
            pids.append(pid)
    return pids


@dataclass
class ProbeEnv:
    """一次探针所需的完整隔离环境与 server 生命周期。"""

    root: Path
    mock_provider_base_url: str
    mcp_server_path: Path
    bin_path: Path
    extra_config: dict[str, Any] = field(default_factory=dict)
    port: int = field(default_factory=free_port)
    process: subprocess.Popen[str] | None = None
    log_path: Path | None = None
    binary_info: BinaryInfo | None = None
    orphans: list[int] = field(default_factory=list)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        self.config_dir = self.root / "config" / "opencode"
        self.data_dir = self.root / "data"
        self.state_dir = self.root / "state"
        self.cache_dir = self.root / "cache"
        self.work_dir = self.root / "work"
        self.home_dir = self.root / "home"
        for path in (
            self.config_dir,
            self.data_dir,
            self.state_dir,
            self.cache_dir,
            self.work_dir,
            self.home_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.skill_dir = self.root / "skills"
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        _write_skill(self.skill_dir)
        self._log_handle: Any = None

    # ------------------------------------------------------------------ config
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def build_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "model": "mock/mock-model",
            "provider": {
                "mock": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Mock",
                    "options": {
                        "baseURL": self.mock_provider_base_url,
                        "apiKey": "probe-key",
                    },
                    "models": {
                        "mock-model": {"name": "mock-model", "status": "active"},
                        "mock-model-b": {"name": "mock-model-b", "status": "active"},
                    },
                }
            },
            "agent": {
                "probe-a": {
                    "description": "probe agent A",
                    "mode": "primary",
                    "prompt": AGENT_A_PROMPT,
                },
                "probe-b": {
                    "description": "probe agent B",
                    "mode": "primary",
                    "prompt": AGENT_B_PROMPT,
                },
            },
            "mcp": {
                "probe": {
                    "type": "local",
                    "command": [
                        sys.executable,
                        str(self.mcp_server_path),
                        "--run-id",
                        self.run_id,
                    ],
                    "enabled": True,
                }
            },
            "skills": {"paths": [str(self.skill_dir)]},
            "share": "disabled",
        }
        extra = dict(self.extra_config)
        providers = extra.pop("provider", None)
        if isinstance(providers, dict):
            config["provider"] = {**config["provider"], **providers}
        config.update(extra)
        return config

    def write_config(self, config: dict[str, Any] | None = None) -> None:
        payload = config if config is not None else self.build_config()
        (self.config_dir / "opencode.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    # ---------------------------------------------------------------- lifecycle
    def _server_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "HOME": str(self.home_dir),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.data_dir),
            "XDG_STATE_HOME": str(self.state_dir),
            "XDG_CACHE_HOME": str(self.cache_dir),
            "OPENCODE_PERMISSION": '{"*":"allow"}',
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            # 外部技能（~/.claude、~/.agents）会污染探针，使 skill 列表不可复现。
            "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
            "WITTY_PROBE_RUN_ID": self.run_id,
        }

    def start(self, *, rewrite_config: bool = True) -> None:
        if self.binary_info is None:
            self.binary_info = verify_opencode_binary(self.bin_path)
        if rewrite_config:
            self.write_config()
        if self.log_path is None:
            self.log_path = self.root / "serve.log"
        last_error: Exception | None = None
        for _ in range(3):
            if not _port_is_free(self.port):
                self.port = free_port()
            self._log_handle = self.log_path.open("a", encoding="utf-8")
            self.process = subprocess.Popen(
                [
                    str(self.bin_path),
                    "serve",
                    "--port",
                    str(self.port),
                    "--print-logs",
                ],
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=self._server_env(),
                start_new_session=True,
            )
            try:
                self.wait_healthy()
                version = self.health_version()
                if version != PROBE_OPENCODE_VERSION:
                    raise ProbeEnvironmentError(
                        f"/global/health version={version!r}，"
                        f"期望 {PROBE_OPENCODE_VERSION}"
                    )
                self.wait_mcp_ready()
                return
            except Exception as exc:  # 端口冲突需要重试，其余错误原样抛出
                last_error = exc
                tail = self.log_tail()
                self._terminate_group()
                self._close_log()
                if "EADDRINUSE" in tail or "address already in use" in tail.lower():
                    self.port = free_port()
                    continue
                raise ProbeEnvironmentError(
                    f"opencode serve 启动失败: {exc}\n{tail}"
                ) from exc
        raise ProbeEnvironmentError(
            f"opencode serve 连续端口冲突，最后一次错误: {last_error}\n"
            f"{self.log_tail()}"
        )

    def _terminate_group(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)

    def _close_log(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()

    def stop(self, *, cleanup: bool | None = None) -> None:
        self._terminate_group()
        self._close_log()
        orphan_pids = _scan_run_processes(self.run_id)
        for pid in orphan_pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        self.orphans = _scan_run_processes(self.run_id)
        if cleanup is None:
            cleanup = os.environ.get(_KEEP_TMP_ENV) != "1"
        if cleanup:
            shutil.rmtree(self.root, ignore_errors=True)

    def restart(self, *, rewrite_config: bool = False) -> None:
        self.stop(cleanup=False)
        self.start(rewrite_config=rewrite_config)

    def wait_healthy(self, timeout: float = _READY_TIMEOUT) -> dict[str, Any]:
        deadline = time.time() + timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise ProbeEnvironmentError(
                    f"opencode serve exited with rc={self.process.returncode}: "
                    f"{self.log_tail()}"
                )
            try:
                response = httpx.get(f"{self.base_url}/global/health", timeout=2.0)
                if response.status_code == 200:
                    return dict(response.json())
            except Exception as exc:
                last_error = exc
            time.sleep(0.2)
        raise ProbeEnvironmentError(
            f"opencode serve not healthy: {last_error}; {self.log_tail()}"
        )

    def health_version(self) -> str:
        payload = self.wait_healthy()
        return str(payload.get("version", ""))

    def wait_mcp_ready(
        self, name: str = "probe", timeout: float = _MCP_TIMEOUT
    ) -> None:
        deadline = time.time() + timeout
        last: Any = None
        with httpx.Client(base_url=self.base_url, timeout=10.0) as client:
            while time.time() < deadline:
                response = client.get("/mcp")
                response.raise_for_status()
                last = response.json()
                entry = last.get(name) if isinstance(last, dict) else None
                if isinstance(entry, dict) and entry.get("status") == "connected":
                    return
                time.sleep(0.1)
        raise ProbeEnvironmentError(
            f"MCP {name!r} 未进入 connected 状态: {last}; {self.log_tail()}"
        )

    def log_tail(self, lines: int = 60) -> str:
        if self.log_path is None or not self.log_path.exists():
            return "<no log>"
        content = self.log_path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(content.splitlines()[-lines:])

    # -------------------------------------------------------------------- http
    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base_url, timeout=60.0)

    @staticmethod
    def _params(directory: Path | None) -> dict[str, str] | None:
        return {"directory": str(directory)} if directory is not None else None

    def create_session(
        self,
        client: httpx.Client,
        *,
        directory: Path | None = None,
        title: str = "probe",
    ) -> str:
        response = client.post(
            "/session", params=self._params(directory), json={"title": title}
        )
        response.raise_for_status()
        return str(response.json()["id"])

    def prompt(
        self,
        client: httpx.Client,
        session_id: str,
        *,
        text: str,
        agent: str | None = None,
        model: tuple[str, str] | None = None,
        tools: dict[str, bool] | None = None,
        directory: Path | None = None,
        message_id: str | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if agent is not None:
            body["agent"] = agent
        if model is not None:
            body["model"] = {"providerID": model[0], "modelID": model[1]}
        if tools is not None:
            body["tools"] = tools
        if message_id is not None:
            body["messageID"] = message_id
        return client.post(
            f"/session/{session_id}/prompt_async",
            params=self._params(directory),
            json=body,
        )

    def wait_idle(
        self,
        client: httpx.Client,
        session_id: str,
        timeout: float = _PROMPT_TIMEOUT,
        *,
        directory: Path | None = None,
        require_busy: bool = True,
    ) -> None:
        """等待 session 变为 idle（``/session/status``）。"""
        deadline = time.time() + timeout
        params = self._params(directory)
        saw_busy = not require_busy
        while time.time() < deadline:
            status = client.get("/session/status", params=params)
            status.raise_for_status()
            payload = status.json()
            entry = payload.get(session_id) if isinstance(payload, dict) else None
            busy = bool(entry) and (
                entry.get("type") if isinstance(entry, dict) else entry
            ) in {"busy", "retry"}
            if busy:
                saw_busy = True
            elif saw_busy:
                return
            time.sleep(0.05)
        raise TimeoutError(
            f"session {session_id} did not become idle; {self.log_tail()}"
        )

    def prompt_and_wait(
        self,
        client: httpx.Client,
        session_id: str,
        *,
        directory: Path | None = None,
        timeout: float = _PROMPT_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        """prompt 并等待这一轮完成（以 assistant 消息带 ``time.completed`` 为准）。"""
        before = self.completed_assistant_ids(client, session_id)
        response = self.prompt(client, session_id, directory=directory, **kwargs)
        response.raise_for_status()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.completed_assistant_ids(client, session_id) - before:
                return
            time.sleep(0.05)
        raise TimeoutError(
            f"session {session_id} turn did not finish; "
            f"status={self.status(client, directory=directory)}; {self.log_tail()}"
        )

    def completed_assistant_ids(
        self, client: httpx.Client, session_id: str
    ) -> set[str]:
        ids: set[str] = set()
        for message in self.messages(client, session_id):
            info = message.get("info") or {}
            if info.get("role") == "assistant" and (info.get("time") or {}).get(
                "completed"
            ):
                ids.add(str(info.get("id")))
        return ids

    def messages(
        self,
        client: httpx.Client,
        session_id: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        response = client.get(f"/session/{session_id}/message", params=params)
        response.raise_for_status()
        return list(response.json())

    def user_message_ids(self, client: httpx.Client, session_id: str) -> list[str]:
        return [
            str(message["info"]["id"])
            for message in self.messages(client, session_id)
            if (message.get("info") or {}).get("role") == "user"
        ]

    def status(
        self, client: httpx.Client, *, directory: Path | None = None
    ) -> dict[str, Any]:
        response = client.get("/session/status", params=self._params(directory))
        response.raise_for_status()
        return dict(response.json())

    def session(self, client: httpx.Client, session_id: str) -> dict[str, Any]:
        response = client.get(f"/session/{session_id}")
        response.raise_for_status()
        return dict(response.json())

    def server_directory(self, client: httpx.Client) -> str:
        response = client.get("/path")
        response.raise_for_status()
        return str(response.json().get("directory", ""))

    def mcp_status(
        self, client: httpx.Client, *, directory: Path | None = None
    ) -> dict[str, Any]:
        response = client.get("/mcp", params=self._params(directory))
        response.raise_for_status()
        return dict(response.json())

    def skills(
        self, client: httpx.Client, directory: Path | None = None
    ) -> list[dict[str, Any]]:
        response = client.get("/skill", params=self._params(directory))
        response.raise_for_status()
        return list(response.json())

    def revert(
        self,
        client: httpx.Client,
        session_id: str,
        message_id: str,
        *,
        directory: Path | None = None,
    ) -> None:
        response = client.post(
            f"/session/{session_id}/revert",
            params=self._params(directory),
            json={"messageID": message_id},
        )
        response.raise_for_status()

    def unrevert(
        self,
        client: httpx.Client,
        session_id: str,
        *,
        directory: Path | None = None,
    ) -> None:
        response = client.post(
            f"/session/{session_id}/unrevert", params=self._params(directory)
        )
        response.raise_for_status()

    def abort(
        self,
        client: httpx.Client,
        session_id: str,
        *,
        directory: Path | None = None,
    ) -> None:
        response = client.post(
            f"/session/{session_id}/abort", params=self._params(directory)
        )
        response.raise_for_status()

    def abort_and_wait(
        self,
        client: httpx.Client,
        session_id: str,
        timeout: float = 30.0,
        *,
        directory: Path | None = None,
    ) -> None:
        self.abort(client, session_id, directory=directory)
        self.wait_idle(
            client,
            session_id,
            timeout=timeout,
            directory=directory,
            require_busy=False,
        )


@dataclass(frozen=True)
class SseRecord:
    payload: dict[str, Any]
    sse_id: str | None
    event_name: str | None
    received_at: float = field(default_factory=time.monotonic)


class EventCollector:
    """后台线程消费 SSE，解析 ``id`` / ``event`` / ``data`` 帧。

    ``path`` 默认 ``/event``（legacy）；``/api/event`` 与
    ``/api/session/{id}/event`` 通过 ``path`` 复用同一套解析逻辑。
    """

    def __init__(
        self,
        base_url: str,
        *,
        directory: Path | None = None,
        after: str | None = None,
        path: str = "/event",
    ) -> None:
        self._base_url = base_url
        self._directory = directory
        self._after = after
        self._path = path
        self._records: list[SseRecord] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._response: httpx.Response | None = None
        self._client: httpx.Client | None = None

    @property
    def error(self) -> BaseException | None:
        with self._lock:
            return self._error

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        params: dict[str, str] = {}
        if self._directory is not None:
            params["directory"] = str(self._directory)
        if self._after is not None:
            params["after"] = self._after
        try:
            self._client = httpx.Client(base_url=self._base_url, timeout=None)
            with self._client.stream(
                "GET", self._path, params=params or None
            ) as response:
                self._response = response
                response.raise_for_status()
                sse_id: str | None = None
                event_name: str | None = None
                data_lines: list[str] = []
                for line in response.iter_lines():
                    if self._stop.is_set():
                        return
                    if line == "":
                        if data_lines:
                            raw = "\n".join(data_lines)
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                payload = None
                            if isinstance(payload, dict):
                                with self._lock:
                                    self._records.append(
                                        SseRecord(payload, sse_id, event_name)
                                    )
                        sse_id = None
                        event_name = None
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    field_name, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if field_name == "data":
                        data_lines.append(value)
                    elif field_name == "id":
                        sse_id = value
                    elif field_name == "event":
                        event_name = value
        except BaseException as exc:
            if not self._stop.is_set():
                with self._lock:
                    self._error = exc

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        response = self._response
        client = self._client
        if response is not None:
            with contextlib.suppress(Exception):
                response.close()
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        error = self.error
        if error is not None:
            with self._lock:
                self._error = None
            raise error

    def records(self) -> list[SseRecord]:
        with self._lock:
            return list(self._records)

    def snapshot(self) -> list[dict[str, Any]]:
        return [record.payload for record in self.records()]

    def wait_for(
        self,
        predicate: Any,
        timeout: float = 30.0,
        *,
        description: str = "expected event",
    ) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for payload in self.snapshot():
                if predicate(payload):
                    return payload
            error = self.error
            if error is not None:
                raise AssertionError(f"{description}: EventCollector 失败: {error!r}")
            time.sleep(0.05)
        recent = self.snapshot()[-10:]
        raise TimeoutError(
            f"{description} did not arrive in {timeout}s; "
            f"last events={[e.get('type') for e in recent]}; "
            f"collector_error={self.error!r}"
        )

    def wait_connected(self, timeout: float = 10.0) -> None:
        self.wait_for(
            lambda payload: payload.get("type") == "server.connected",
            timeout=timeout,
            description="server.connected",
        )

    def wait_for_type(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        def matches(payload: dict[str, Any]) -> bool:
            if payload.get("type") != event_type:
                return False
            if session_id is None:
                return True
            properties = payload.get("properties") or {}
            return properties.get("sessionID") == session_id

        return self.wait_for(
            matches, timeout=timeout, description=f"event {event_type}"
        )

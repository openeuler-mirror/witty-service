from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

logger = logging.getLogger(__name__)

_recovery_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task[Any]] = set()


def _log_prefix(agent_id: str | None = None, session_id: str | None = None) -> str:
    parts = []
    if agent_id:
        parts.append(f"agent_id={agent_id}")
    if session_id:
        parts.append(f"session_id={session_id}")
    if parts:
        return f"[{', '.join(parts)}] "
    return ""


def redact_start_payload(payload: Any) -> Any:
    """递归脱敏 `/agent/start` payload 中的敏感字段（如 ``api_key``），用于日志打印。

    各 runtime 子对象（model / dsh / opencode / openclaw ...）可能内嵌凭据，
    打印前统一遮蔽，避免密钥进日志。
    """
    if isinstance(payload, dict):
        return {
            key: ("***" if key == "api_key" else redact_start_payload(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_start_payload(value) for value in payload]
    return payload


from witty_service.adapter.http_client import AdaptorHttpClient
from witty_service.adapter.websocket_client import WebSocketClient
from witty_service.adapter.websocket_client_pool import (
    AdaptorEndpoint,
    WebSocketClientPool,
)
from witty_service.adapter.websocket_protocol import OutboundMessage
from witty_service.domain.enums import AgentStatus, can_transition
from witty_service.domain.errors import DomainError, agent_not_found
from witty_service.domain.field_limits import (
    validate_agent_description,
    validate_agent_name,
)
from witty_service.persistence.orm import MessageStatus
from witty_service.persistence.repositories import AgentRecord, SessionRecord
from witty_service.sandbox.base import (
    SANDBOX_NOT_FOUND,
    SandboxHandle,
)

# 沙箱进程存活判定与 local_process backend 共用同一套 /proc 解析：两者判的是同一
# 件事（这个 pid 是不是还活着），分成两套实现迟早会漂移。
from witty_service.sandbox.local_process import read_proc_stat
from witty_service.sandbox.ports import find_free_port, port_is_bindable
from witty_service.storage.runtime_backup import RuntimeBackupStore

from .artifact_paths import normalize_artifact_event
from .runtime_config import DshConfig, OpenclawConfig, OpencodeConfig, RuntimeConfig
from .session_manager import SessionManager

INVALID_AGENT_TRANSITION = "INVALID_AGENT_TRANSITION"
SANDBOX_STATE_NOT_FOUND = "SANDBOX_STATE_NOT_FOUND"
AGENT_NOT_RUNNING = "AGENT_NOT_RUNNING"
AGENT_CREATE_FAILED = "AGENT_CREATE_FAILED"
AGENT_PAUSE_FAILED = "AGENT_PAUSE_FAILED"
AGENT_RESUME_FAILED = "AGENT_RESUME_FAILED"
AGENT_DELETE_FAILED = "AGENT_DELETE_FAILED"
RUNTIME_BACKUP_NOT_FOUND = "RUNTIME_BACKUP_NOT_FOUND"
SANDBOX_NOT_READY = "SANDBOX_NOT_READY"
RUNTIME_START_FAILED = "RUNTIME_START_FAILED"
SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
SKILL_INSTALL_RECORD_FAILED = "SKILL_INSTALL_RECORD_FAILED"
SKILL_UNINSTALL_RECORD_FAILED = "SKILL_UNINSTALL_RECORD_FAILED"
SKILL_SYNC_FAILED = "SKILL_SYNC_FAILED"
AGENT_SKILL_INSTALL_FAILED = "AGENT_SKILL_INSTALL_FAILED"
AGENT_SKILL_UNINSTALL_FAILED = "AGENT_SKILL_UNINSTALL_FAILED"

SKILL_INSTALL_TIMEOUT_SECONDS = 180.0

# 纯传输事件：实时推送给前端、但不落库（见 consume_ws 中的落库分支）。
# ``tool.call.delta`` 是工具执行过程中的增量输出，前端只在流式过程中消费它；
# 终态 ``tool.call.response`` 已携带完整输出，落库只会让每次 exec 多出成百上千行
# 永不回读的载荷，并在时间线上划出一道「压缩后会消失」的假分段边界。
TRANSIENT_EVENT_TYPES = frozenset({"tool.call.delta"})

# 流式事件的落库批窗口。逐条 commit 在默认 journal 模式下每次都要 fsync
# （本机 ext4 实测 4.8 ms/事件，而 opencode 按 token 下发事件、峰值 151~176 事件/秒），
# 会把 asyncio 事件循环按秒级堵死 —— 见 persistence.db._configure_sqlite_engine 的说明。
# 合并成"每窗口一次提交"后，单次提交仍是同步的，但窗口内最多只阻塞一次。
PERSIST_BATCH_MAX_EVENTS = 64
PERSIST_BATCH_INTERVAL_S = 0.25

INTERRUPTION_PREFIX = """[CRITICAL SYSTEM INSTRUCTION - OVERRIDE ALL PREVIOUS CONTEXT]

The assistant's previous response in the conversation history was INTERRUPTED and INCOMPLETE before being sent to you.

You MUST follow these rules with HIGHEST PRIORITY:

1. IGNORE the ENTIRE interrupted assistant message completely - treat it as if it never existed
2. DO NOT continue, complete, reference, or acknowledge that interrupted response in ANY way
3. DO NOT use phrases like "continuing from", "as I was saying", "to complete my previous thought"
4. Answer ONLY and DIRECTLY the user's message below, starting from a fresh response

The user's current message (ignore everything before this):

"""


@dataclass(slots=True, frozen=True)
class AgentCreateRequest:
    name: str
    sandbox_type: str
    adapter_type: str
    idle_timeout_seconds: int
    description: str = ""
    sandbox_id: str | None = None
    model_id: str | None = None
    mcp_server_list: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class AgentCreateResult:
    agent: AgentRecord


class SandboxState(Protocol):
    handle: SandboxHandle
    adapter_base_url: str


class AgentRepository(Protocol):
    def create_agent_with_id(
        self,
        *,
        agent_id: str,
        name: str,
        sandbox_type: str,
        adapter_type: str,
        workspace_path: str,
        idle_timeout_seconds: int,
        description: str = "",
        status: AgentStatus = AgentStatus.creating,
        sandbox_id: str | None = None,
        model_id: str | None = None,
        mcp_server_list: list[str] | None = None,
        last_active_at: Any | None = None,
    ) -> AgentRecord: ...

    def get_agent(self, agent_id: str) -> AgentRecord | None: ...

    def list_agents_needing_recovery(
        self,
        sandbox_type: str | None = None,
        status_filter: list[AgentStatus] | None = None,
    ) -> list[AgentRecord]: ...

    def update_agent_status(
        self,
        agent_id: str,
        status: AgentStatus,
        updated_at: Any | None = None,
    ) -> AgentRecord: ...

    def save_sandbox_state(
        self,
        agent_id: str,
        sandbox_payload_json: dict[str, Any],
        adapter_base_url: str,
        adapter_ready: bool = True,
        last_error: str | None = None,
    ) -> SandboxState: ...

    def get_sandbox_state(self, agent_id: str) -> SandboxState | None: ...

    def create_message(
        self,
        *,
        agent_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> str: ...

    def create_message_event_with_retry(
        self,
        *,
        agent_id: str,
        session_id: str,
        event_type: str,
        payload_json: dict[str, Any],
        seq_no: int,
        message_id: str | None = None,
        max_retries: int = 5,
    ) -> tuple[str, int]: ...

    def create_assistant_message_and_bind_events(
        self,
        *,
        agent_id: str,
        session_id: str,
        content: str,
        event_ids: list[str],
        metadata_json: dict[str, Any] | None = None,
    ) -> str: ...

    def get_last_assistant_status(self, session_id: str) -> str | None: ...

    def get_first_user_message(self, session_id: str) -> str | None: ...

    def update_session_metadata(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> Any: ...

    def update_message_content(self, message_id: str, content: str) -> None: ...

    def update_message_stream_at(self, message_id: str) -> None: ...

    def update_message_status(self, message_id: str, status: Any) -> None: ...

    def find_stale_generating_messages(
        self, stale_threshold_seconds: int
    ) -> list[Any]: ...

    def find_last_assistant_message_for_session(
        self, session_id: str
    ) -> Any | None: ...

    def update_session_runtime_identity(
        self,
        *,
        session_id: str,
        runtime_type: str,
        runtime_session_id: str,
        runtime_session_key: str,
    ) -> SessionRecord: ...

    def compact_message_delta_events(self, message_id: str) -> None: ...

    def delete_agent(self, agent_id: str) -> None: ...

    def upsert_builtin_skill(
        self,
        *,
        skill_id: str,
        skill_name: str,
        metadata: dict[str, Any],
        skill_source: str | None = None,
        relative_path: str | None = None,
    ) -> Any: ...

    def upsert_installed_agent_skill(
        self,
        *,
        agent_id: str,
        skill_id: str,
        source_type: str,
        skill_name: str,
        repo_id: str | None = None,
        relative_path: str | None = None,
        metadata: dict[str, Any] | None = None,
        skill_source: str | None = None,
        skill_md_url: str | None = None,
        installed_at: datetime | None = None,
    ) -> Any: ...

    def replace_installed_agent_skills_from_runtime(
        self,
        *,
        agent_id: str,
        skills: list[dict[str, Any]],
    ) -> None: ...

    def get_model(self, model_id: str) -> Any | None: ...


class WorkspaceStore(Protocol):
    def init_workspace(self, agent_id: str) -> Path: ...

    def cleanup_workspace(self, agent_id: str) -> None: ...


class SandboxBackend(Protocol):
    def start(
        self,
        *,
        agent_id: str,
        workspace_path: str,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> SandboxHandle: ...

    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None: ...

    def endpoint(self, handle: SandboxHandle | str, **kwargs: Any) -> Any: ...

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None: ...


class SessionStreamRegistry:
    """Broadcast stream events to multiple SSE subscribers per session.

    When a client disconnects (page refresh), the background WS consumer
    keeps running so the generation completes and events are persisted.
    Reconnecting clients get buffered events first, then live events.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict | None]]] = {}
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._terminated: set[str] = set()
        self._generation: dict[str, int] = {}

    def start_stream(self, session_id: str) -> int:
        self.end_stream(session_id)
        gen = self._generation.get(session_id, 0) + 1
        self._generation[session_id] = gen
        self._subscribers[session_id] = []
        self._buffers[session_id] = []
        self._terminated.discard(session_id)
        return gen

    def push_event(
        self, session_id: str, event: dict[str, Any], generation: int
    ) -> None:
        if self._generation.get(session_id) != generation:
            return  # Stale generation — ignore
        if session_id in self._buffers:
            self._buffers[session_id].append(event)
        for q in self._subscribers.get(session_id, []):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    def end_stream(self, session_id: str) -> None:
        self._terminated.add(session_id)
        for q in self._subscribers.get(session_id, []):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)  # sentinel

    def is_active(self, session_id: str) -> bool:
        return session_id in self._subscribers and session_id not in self._terminated

    def get_buffered_events(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._buffers.get(session_id, []))

    def subscribe(self, session_id: str) -> asyncio.Queue[dict | None]:
        q: asyncio.Queue[dict | None] = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(q)
        return q

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[dict | None]) -> None:
        subs = self._subscribers.get(session_id, [])
        if queue in subs:
            subs.remove(queue)

    def has_subscribers(self, session_id: str) -> bool:
        return bool(self._subscribers.get(session_id, []))

    def cleanup(self, session_id: str) -> None:
        self._subscribers.pop(session_id, None)
        self._buffers.pop(session_id, None)
        self._terminated.discard(session_id)
        self._generation.pop(session_id, None)


_stream_registry = SessionStreamRegistry()


class AgentManager:
    _RUNTIME_CONFIGS: ClassVar[dict[str, RuntimeConfig]] = {
        "opencode": OpencodeConfig(),
        "openclaw": OpenclawConfig(),
        "dsh": DshConfig(),
    }

    def __init__(
        self,
        *,
        repository: AgentRepository,
        session_manager: SessionManager,
        workspace_store: WorkspaceStore,
        sandbox_backend: SandboxBackend,
        ws_client_pool: WebSocketClientPool | None = None,
    ) -> None:
        self._repository = repository
        self._session_manager = session_manager
        self._workspace_store = workspace_store
        self._sandbox_backend = sandbox_backend
        self._ws_client_pool = ws_client_pool or WebSocketClientPool()
        self._logger = logging.getLogger(__name__)

    def _get_runtime_config(self, adapter_type: str) -> RuntimeConfig:
        config = self._RUNTIME_CONFIGS.get(adapter_type)
        if config is None:
            raise DomainError(
                code="UNSUPPORTED_RUNTIME_TYPE",
                message=f"Unsupported runtime adapter type: {adapter_type}",
                details={"adapter_type": adapter_type},
            )
        return config

    def _resolve_model_info(self, model_id: str | None) -> dict[str, Any]:
        """从仓库解析模型信息，不存在时返回空 dict."""
        if model_id is None:
            return {}
        model = self._repository.get_model(model_id)
        if model is None:
            return {}
        return {
            "name": model.name,
            "provider": model.provider,
            "api_key": model.api_key,
            "api_base_url": model.api_base_url,
            "compatibility": model.compatibility,
            "max_tokens": getattr(model, "max_tokens", None),
        }

    def _get_runtime_gateway_port(self, agent_id: str, adapter_type: str) -> int | None:
        """从 sandbox_state 提取 runtime 上一次用的网关端口（无网关端口的 runtime 无此概念）."""
        config = self._get_runtime_config(adapter_type)
        if not config.uses_gateway_port():
            return None
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is None:
            return None
        metadata = sandbox_state.sandbox_payload_json.get("metadata", {})
        port_val = metadata.get(config.port_metadata_key())
        return int(port_val) if isinstance(port_val, int) else None

    def _get_sandbox_port(self, agent_id: str) -> int | None:
        """沙箱自身（witty-agent-server）上一次的监听端口，即 metadata["port"]."""
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is None:
            return None
        metadata = sandbox_state.sandbox_payload_json.get("metadata") or {}
        port_val = metadata.get("port")
        return int(port_val) if isinstance(port_val, int) else None

    def _select_gateway_port(
        self, *, adapter_type: str, saved_port: int | None
    ) -> int | None:
        """选定 runtime 的网关端口：优先复用上一次的取值，真被占用时才换新的。

        没有网关端口的 runtime（dsh）返回 None——调用方据此跳过端口分配与落库，
        不在 metadata 里留下一个没有任何进程监听的端口号。
        """
        if not self._get_runtime_config(adapter_type).uses_gateway_port():
            return None
        if saved_port is not None and not self._is_port_in_use(saved_port):
            return saved_port
        return find_free_port()

    def _is_port_in_use(self, port: int) -> bool:
        """端口是否真的被占用（内核残留 socket 不算，判定见 sandbox.ports）."""
        return not port_is_bindable(port)

    def _get_agent_profile(self, agent_id: str) -> str:
        return agent_id

    def list_agent_skills(self, agent_id: str) -> list[dict[str, Any]]:
        """查询当前 agent 对应 runtime 支持的 skills。"""
        builtin_skills = self._fetch_agent_skills_from_runtime(agent_id)
        try:
            self._sync_builtin_skills(agent_id, builtin_skills)
        except Exception:
            self._logger.warning(
                "Failed to sync builtin skills into DB: agent_id=%s",
                agent_id,
                exc_info=True,
            )
        self._logger.info(
            "Listed agent skills successfully: agent_id=%s skill_count=%s",
            agent_id,
            len(builtin_skills),
        )
        return builtin_skills

    def sync_installed_agent_skills(self, agent_id: str) -> list[dict[str, Any]]:
        """Fetch installed skills from runtime and replace DB records in one transaction."""
        builtin_skills = self._fetch_agent_skills_from_runtime(agent_id)
        self._repository.replace_installed_agent_skills_from_runtime(
            agent_id=agent_id,
            skills=builtin_skills,
        )
        self._logger.info(
            "Synced installed skills successfully: agent_id=%s skill_count=%s",
            agent_id,
            len(builtin_skills),
        )
        return builtin_skills

    def _fetch_agent_skills_from_runtime(self, agent_id: str) -> list[dict[str, Any]]:
        """Fetch current runtime-visible skills for one agent."""
        sandbox_state = self._get_sandbox_state(agent_id)
        self._logger.info(
            "Listing agent skills: agent_id=%s base_url=%s",
            agent_id,
            sandbox_state.adapter_base_url,
        )

        client: httpx.Client | None = None
        try:
            client = httpx.Client(base_url=sandbox_state.adapter_base_url, timeout=30.0)

            for attempt in range(10):
                try:
                    # 必须带 id：runtime 的 skill 目录按 agent_workspace_path(<agent_id>)
                    # 推导；缺省会回退到 "_default"/"main"，读到错误的空目录。
                    response = client.get("/agent/skills", params={"id": agent_id})
                    response.raise_for_status()
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 502 and attempt < 9:
                        self._logger.debug(
                            "Skills endpoint not ready yet (attempt %d/10): agent_id=%s",
                            attempt + 1,
                            agent_id,
                        )
                        time.sleep(1)
                        continue
                    raise
                except httpx.ConnectError:
                    if attempt < 9:
                        self._logger.debug(
                            "Skills endpoint connection failed (attempt %d/10): agent_id=%s",
                            attempt + 1,
                            agent_id,
                        )
                        time.sleep(1)
                        continue
                    raise

            payload = response.json()

            if isinstance(payload, list):
                skills = payload
            else:
                skills = payload.get("skills", []) if isinstance(payload, dict) else []

            if not isinstance(skills, list):
                self._logger.warning(
                    "Agent skills response has invalid format: agent_id=%s payload_type=%s",
                    agent_id,
                    type(payload).__name__,
                )
                return []

            return [item for item in skills if isinstance(item, dict)]
        except Exception:
            self._logger.exception("Failed to list agent skills: agent_id=%s", agent_id)
            raise
        finally:
            if client is not None:
                client.close()

    def _sync_builtin_skills(self, agent_id: str, skills: list[dict[str, Any]]) -> None:
        for item in skills:
            skill_name = item.get("name")
            if not isinstance(skill_name, str) or not skill_name.strip():
                continue

            normalized_name = skill_name.strip()
            skill_source = item.get("source")
            source_value = skill_source if isinstance(skill_source, str) else None
            file_path = item.get("filePath")
            relative_path = file_path if isinstance(file_path, str) else None
            skill_id = self._build_builtin_skill_id(agent_id, normalized_name)

            self._repository.upsert_builtin_skill(
                skill_id=skill_id,
                skill_name=normalized_name,
                metadata=dict(item),
                skill_source=source_value,
                relative_path=relative_path,
            )
            self._repository.upsert_installed_agent_skill(
                agent_id=agent_id,
                skill_id=skill_id,
                source_type="builtin",
                repo_id=None,
                skill_name=normalized_name,
                relative_path=relative_path,
                metadata=dict(item),
                skill_source=source_value,
            )

    def _build_builtin_skill_id(self, agent_id: str, skill_name: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"builtin:{agent_id}:{skill_name}"))

    async def install_agent_skill(
        self,
        agent_id: str,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        skill_source: str | None = None,
    ) -> dict[str, Any]:
        """下发 skill 到 runtime。"""
        agent = self._get_agent(agent_id)

        if agent.status is AgentStatus.paused:
            agent = await self.resume_agent(agent_id)
        elif agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to install skills.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                request_body: dict[str, Any] = {"skill_name": skill_name}
                if source_type:
                    request_body["source_type"] = source_type
                if source_path:
                    request_body["source_path"] = source_path
                if skill_source:
                    request_body["skill_source"] = skill_source
                payload = await adaptor_client.post(
                    f"/agent/skills/install?id={agent_id}",
                    json=request_body,
                    timeout=SKILL_INSTALL_TIMEOUT_SECONDS,
                )
            except httpx.HTTPError as exc:
                details = self._build_runtime_skill_error_details(
                    agent_id=agent_id,
                    skill_name=skill_name,
                    exc=exc,
                )
                raise DomainError(
                    code=AGENT_SKILL_INSTALL_FAILED,
                    message="Failed to install skill on runtime.",
                    status_code=self._runtime_skill_error_status_code(exc),
                    details=details,
                ) from exc
        finally:
            await adaptor_client.close()

        if not isinstance(payload, dict):
            return {"status": "accepted"}
        return payload

    async def uninstall_agent_skill(
        self,
        agent_id: str,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        runtime_source: str | None = None,
    ) -> dict[str, Any]:
        """从 runtime 卸载 skill。"""
        agent = self._get_agent(agent_id)

        if agent.status is AgentStatus.paused:
            agent = await self.resume_agent(agent_id)
        elif agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to uninstall skills.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                request_body: dict[str, Any] = {"skill_name": skill_name}
                if source_type:
                    request_body["source_type"] = source_type
                if source_path:
                    request_body["source_path"] = source_path
                if runtime_source:
                    request_body["runtime_source"] = runtime_source
                payload = await adaptor_client.post(
                    f"/agent/skills/uninstall?id={agent_id}",
                    json=request_body,
                )
            except httpx.HTTPError as exc:
                details = self._build_runtime_skill_error_details(
                    agent_id=agent_id,
                    skill_name=skill_name,
                    exc=exc,
                )
                raise DomainError(
                    code=AGENT_SKILL_UNINSTALL_FAILED,
                    message="Failed to uninstall skill on runtime.",
                    status_code=self._runtime_skill_error_status_code(exc),
                    details=details,
                ) from exc
        finally:
            await adaptor_client.close()

        if not isinstance(payload, dict):
            return {"status": "accepted"}
        return payload

    def create_agent(self, request: AgentCreateRequest) -> AgentCreateResult:
        validate_agent_name(request.name)
        validate_agent_description(request.description)

        agent_id = str(uuid4())
        prefix = _log_prefix(agent_id=agent_id)
        logger.info(
            f"{prefix}Creating agent: name=%s sandbox_type=%s",
            request.name,
            request.sandbox_type,
        )

        profile_name = agent_id
        gateway_port = self._select_gateway_port(
            adapter_type=request.adapter_type, saved_port=None
        )
        logger.info(
            f"{prefix}Using profile: {profile_name}, gateway_port: {gateway_port}"
        )

        workspace_path = str(self._workspace_store.init_workspace(agent_id))
        logger.info(f"{prefix}Workspace initialized: path=%s", workspace_path)
        sandbox_handle: SandboxHandle | None = None
        try:
            self._create_agent_record(
                agent_id=agent_id,
                request=request,
                workspace_path=workspace_path,
            )
            logger.info(f"{prefix}Agent record created, starting sandbox...")
            runtime_config = self._get_runtime_config(request.adapter_type)
            sandbox_handle = self._sandbox_backend.start(
                agent_id=agent_id,
                workspace_path=workspace_path,
                env=runtime_config.build_env(),
                image_tag=runtime_config.adapter_type,
                memory_limit=runtime_config.memory_limit,
            )
            logger.info(
                f"{prefix}Sandbox started: sandbox_id=%s", sandbox_handle.sandbox_id
            )
            adapter_endpoint = self._sandbox_backend.endpoint(sandbox_handle)
            logger.info(
                f"{prefix}Adapter endpoint ready: url=%s", adapter_endpoint.base_url
            )
            sandbox_payload = self._sandbox_handle_payload(sandbox_handle)
            self._store_gateway_port(
                sandbox_payload,
                adapter_type=request.adapter_type,
                gateway_port=gateway_port,
            )
            self._repository.save_sandbox_state(
                agent_id,
                sandbox_payload_json=sandbox_payload,
                adapter_base_url=adapter_endpoint.base_url,
                adapter_ready=True,
            )
            logger.info(f"{prefix}Sandbox state saved to database")

            logger.info(f"{prefix}Waiting for sandbox health check...")
            client: httpx.Client | None = None
            for i in range(30):
                try:
                    client = httpx.Client(
                        base_url=adapter_endpoint.base_url, timeout=5.0
                    )
                    response = client.get("/ping")
                    if response.status_code == 200:
                        logger.info(f"{prefix}Sandbox ready after %d attempts", i + 1)
                        break
                except Exception as exc:
                    logger.debug(
                        f"{prefix}Health check attempt %d failed: %s", i + 1, exc
                    )
                    pass
                finally:
                    if client is not None:
                        client.close()
                time.sleep(1)
            else:
                logger.error(f"{prefix}Sandbox health check timeout after 30 attempts")
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Sandbox health check timeout.",
                    details={"agent_id": agent_id},
                )

            logger.info(f"{prefix}Calling /agent/start...")
            client = httpx.Client(base_url=adapter_endpoint.base_url, timeout=120.0)
            try:
                start_payload = self._build_agent_start_payload(
                    adapter_type=request.adapter_type,
                    model_id=request.model_id,
                    agent_key=profile_name,
                    gateway_port=gateway_port,
                )
                logger.debug(
                    f"{prefix}/agent/start payload: %s",
                    redact_start_payload(start_payload),
                )
                start_response = client.post("/agent/start", json=start_payload)
                start_response.raise_for_status()
                logger.info(
                    f"{prefix}/agent/start succeeded: status=%d",
                    start_response.status_code,
                )
                started_agent = start_response.json()
                remote_runtime_agent_id = started_agent.get("id")
                if (
                    not isinstance(remote_runtime_agent_id, str)
                    or not remote_runtime_agent_id
                ):
                    raise DomainError(
                        code=AGENT_CREATE_FAILED,
                        message="Started agent response missing runtime agent id.",
                        details={"agent_id": agent_id},
                    )
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agent/start failed: %s", exc)
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Failed to start agent.",
                    details=self._describe_http_error(agent_id=agent_id, exc=exc),
                ) from exc
            finally:
                client.close()

            logger.info(f"{prefix}Calling /agent/sessions...")
            client = httpx.Client(base_url=adapter_endpoint.base_url, timeout=30.0)
            try:
                response = client.post(
                    f"/agents/{remote_runtime_agent_id}/sessions", json={}
                )
                response.raise_for_status()
                logger.info(
                    f"{prefix}/agent/sessions succeeded: status=%d",
                    response.status_code,
                )
                session_data = response.json()
                logger.debug(f"{prefix}Session data: %s", session_data)
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agent/sessions failed: %s", exc)
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Failed to create session on agent.",
                    details=self._describe_http_error(agent_id=agent_id, exc=exc),
                ) from exc
            finally:
                client.close()

            running_agent = self._repository.update_agent_status(
                agent_id,
                AgentStatus.running,
            )
            logger.info(f"{prefix}Agent creation complete: status=running")

            # 同步 builtin skills 到数据库
            try:
                self.sync_installed_agent_skills(agent_id)
                logger.info(f"{prefix}Builtin skills synced successfully")
            except Exception:
                logger.warning(
                    f"{prefix}Failed to sync builtin skills, continuing...",
                    exc_info=True,
                )

            return AgentCreateResult(
                agent=replace(running_agent, workspace_path=workspace_path),
            )
        except Exception as exc:
            logger.error(f"{prefix}Agent creation failed: error=%s", exc, exc_info=True)
            cleanup_errors: list[dict[str, str]] = []
            if sandbox_handle is not None:
                self._collect_error(
                    cleanup_errors,
                    "sandbox_cleanup",
                    lambda: self._sandbox_backend.cleanup(sandbox_handle),
                )
            self._collect_error(
                cleanup_errors,
                "agent_delete",
                lambda: self._repository.delete_agent(agent_id),
            )
            self._raise_operation_failed(
                code=AGENT_CREATE_FAILED,
                message="Agent creation failed.",
                agent_id=agent_id,
                cause=exc,
                cleanup_errors=cleanup_errors,
            )

    def _build_agent_start_payload(
        self,
        *,
        adapter_type: str,
        model_id: str | None,
        agent_key: str,
        gateway_port: int | None,
    ) -> dict[str, Any]:
        """构建 /agent/start 请求的 payload（创建 + 恢复通用）。

        通过 RuntimeConfig 策略生成差异化 payload，不再需要硬编码分支。
        """
        config = self._get_runtime_config(adapter_type)
        model_info = self._resolve_model_info(model_id)
        return config.build_start_payload(
            model_id=model_id,
            model_info=model_info,
            agent_key=agent_key,
            gateway_port=gateway_port,
        )

    #: pause 时等待 runtime 优雅停止的超时。
    PAUSE_RUNTIME_TIMEOUT_SECONDS: ClassVar[float] = 30.0

    def pause_agent(self, agent_id: str) -> AgentRecord:
        agent = self._get_agent(agent_id)
        self._ensure_transition(agent, AgentStatus.paused)

        self._stop_runtime_best_effort(agent_id)

        return self._repository.update_agent_status(agent_id, AgentStatus.paused)

    def _stop_runtime_best_effort(self, agent_id: str) -> None:
        """尽力停掉 runtime；失败只记日志，不阻断 pause。"""
        prefix = _log_prefix(agent_id=agent_id)
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is None or not sandbox_state.adapter_base_url:
            logger.info(f"{prefix}No sandbox to stop, marking paused directly")
            return

        client: httpx.Client | None = None
        try:
            # 调用 witty-agent-server /agent/stop 优雅停止运行时
            client = httpx.Client(
                base_url=sandbox_state.adapter_base_url,
                timeout=self.PAUSE_RUNTIME_TIMEOUT_SECONDS,
            )
            response = client.post("/agent/stop", json={})
            if response.status_code >= 400:
                logger.warning(
                    f"{prefix}Runtime /agent/stop returned %s, marking paused anyway: body=%s",
                    response.status_code,
                    response.text[:500],
                )
        except httpx.HTTPError as exc:
            logger.warning(
                f"{prefix}Runtime /agent/stop failed, marking paused anyway: error=%s",
                exc,
                exc_info=True,
            )
        finally:
            if client is not None:
                client.close()

    async def _resume_from_deleted(self, agent_id: str) -> AgentRecord:
        """从 deleted 状态恢复（备份功能暂禁用）"""
        agent = self._get_agent(agent_id)

        # 1. 检查备份（暂注释，备份功能已禁用）
        # backup_store = RuntimeBackupStore()
        # if not backup_store.backup_exists(agent_id, agent.adapter_type):
        #     raise DomainError(
        #         code=RUNTIME_BACKUP_NOT_FOUND,
        #         message="Runtime backup not found.",
        #         details={"agent_id": agent_id},
        #     )
        # backup_store.restore(agent_id, agent.adapter_type)

        # 2. 重建沙箱（不依赖备份），端口沿用上一次的取值
        gateway_port = await self._restart_sandbox(agent, health_attempts=30)

        # 3. 调用 /agent/start（使用完整的恢复 payload）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                start_payload = self._build_agent_start_payload(
                    adapter_type=agent.adapter_type,
                    model_id=agent.model_id,
                    agent_key=agent.id,
                    gateway_port=gateway_port,
                )
                await adaptor_client.post("/agent/start", json=start_payload)
            except httpx.HTTPStatusError as exc:
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to start runtime.",
                    details=self._describe_http_error(agent_id=agent_id, exc=exc),
                ) from exc
        finally:
            await adaptor_client.close()

        # 4. 更新状态
        return self._repository.update_agent_status(agent_id, AgentStatus.running)

    #: resume 前探活沙箱的超时。pause 只停 runtime、沙箱通常还活着，探活成功即可复用。
    SANDBOX_PROBE_TIMEOUT_SECONDS: ClassVar[float] = 5.0

    async def _probe_sandbox_url(self, base_url: str) -> bool:
        client = AdaptorHttpClient(
            base_url=base_url, timeout=self.SANDBOX_PROBE_TIMEOUT_SECONDS
        )
        try:
            return await client.health_check()
        except Exception:
            return False
        finally:
            await client.close()

    def _store_gateway_port(
        self,
        sandbox_payload: dict[str, Any],
        *,
        adapter_type: str,
        gateway_port: int | None,
    ) -> None:
        """把网关端口写进 sandbox metadata；没有网关端口的 runtime 不写任何端口键。"""
        if gateway_port is None:
            return
        config = self._get_runtime_config(adapter_type)
        metadata = sandbox_payload.setdefault("metadata", {})
        metadata["gateway_port"] = gateway_port
        metadata[config.port_metadata_key()] = gateway_port

    def _save_gateway_port(
        self, *, agent_id: str, gateway_port: int | None, adapter_type: str
    ) -> None:
        """把网关端口写回已有的 sandbox_state（不动 base_url）。"""
        state = self._repository.get_sandbox_state(agent_id)
        if state is None:
            return
        sandbox_payload = dict(state.sandbox_payload_json)
        self._store_gateway_port(
            sandbox_payload,
            adapter_type=adapter_type,
            gateway_port=gateway_port,
        )
        self._repository.save_sandbox_state(
            agent_id,
            sandbox_payload_json=sandbox_payload,
            adapter_base_url=state.adapter_base_url,
            adapter_ready=True,
        )

    async def _wait_for_sandbox_health(self, agent_id: str, *, attempts: int) -> bool:
        """轮询沙箱 /ping，返回是否在 *attempts* 秒内就绪。"""
        prefix = _log_prefix(agent_id=agent_id)
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            for attempt in range(attempts):
                if await adaptor_client.health_check():
                    logger.info(f"{prefix}Sandbox ready after %s attempts", attempt + 1)
                    return True
                await asyncio.sleep(1)
        finally:
            await adaptor_client.close()
        return False

    async def _restart_sandbox(
        self, agent: AgentRecord, *, health_attempts: int = 60
    ) -> int | None:
        """重建沙箱，两个端口都优先复用 sandbox_state 里保存的上一次取值。

        端口一律「先收敛旧进程树、再判是否空闲」：旧树还活着时去探测，只会把自己的
        残留误判成「端口被占」，于是恢复一次白换一个端口。沙箱自身端口的复用交给
        backend 在收敛之后决定（``start(port=...)``）；网关端口在本方法内、``start()``
        返回之后再决定，顺序与前者一致。

        返回 runtime 该用的 gateway_port；没有网关端口的 runtime（dsh）返回 None。
        """
        agent_id = agent.id
        prefix = _log_prefix(agent_id=agent_id)
        state = self._repository.get_sandbox_state(agent_id)
        runtime_config = self._get_runtime_config(agent.adapter_type)
        saved_gateway_port = self._get_runtime_gateway_port(
            agent_id, agent.adapter_type
        )

        # 传旧句柄：backend 会先按身份自证把旧进程树/容器收敛掉，再决定端口。
        sandbox_handle = self._sandbox_backend.start(
            agent_id=agent_id,
            workspace_path=agent.workspace_path,
            env=runtime_config.build_env(),
            image_tag=runtime_config.adapter_type,
            memory_limit=runtime_config.memory_limit,
            previous_handle=state.handle if state is not None else None,
            port=self._get_sandbox_port(agent_id),
        )
        adapter_endpoint = self._sandbox_backend.endpoint(sandbox_handle)
        gateway_port = self._select_gateway_port(
            adapter_type=agent.adapter_type, saved_port=saved_gateway_port
        )
        if saved_gateway_port is not None and gateway_port != saved_gateway_port:
            logger.warning(
                f"{prefix}Saved gateway port {saved_gateway_port} is in use, "
                f"using new port {gateway_port}"
            )
        sandbox_payload = self._sandbox_handle_payload(sandbox_handle)
        self._store_gateway_port(
            sandbox_payload,
            adapter_type=agent.adapter_type,
            gateway_port=gateway_port,
        )
        self._repository.save_sandbox_state(
            agent_id,
            sandbox_payload_json=sandbox_payload,
            adapter_base_url=adapter_endpoint.base_url,
            adapter_ready=True,
        )
        logger.info(
            f"{prefix}Sandbox restarted: base_url=%s gateway_port=%s",
            adapter_endpoint.base_url,
            gateway_port,
        )
        if not await self._wait_for_sandbox_health(agent_id, attempts=health_attempts):
            raise DomainError(
                code=SANDBOX_NOT_READY,
                message="Sandbox health check timeout.",
                details={"agent_id": agent_id},
            )
        return gateway_port

    async def _ensure_sandbox_ready_for_resume(self, agent: AgentRecord) -> int | None:
        """确保 agent 有一个健康的沙箱，返回 runtime 该用的 gateway_port。

        1. 先用数据库里保存的 base_url 做一次**短超时**探活：pause 的语义是只停 runtime、
           沙箱留下，所以正常情况下探活成功，直接复用即可秒级恢复。
        2. 探活失败说明沙箱确实没了（服务重启 / 机器重启 / 进程组被杀），重建沙箱。
        """
        agent_id = agent.id
        prefix = _log_prefix(agent_id=agent_id)

        state = self._repository.get_sandbox_state(agent_id)
        base_url = state.adapter_base_url if state is not None else None

        if base_url and await self._probe_sandbox_url(base_url):
            saved_port = self._get_runtime_gateway_port(agent_id, agent.adapter_type)
            gateway_port = self._select_gateway_port(
                adapter_type=agent.adapter_type, saved_port=saved_port
            )
            if saved_port is not None and gateway_port != saved_port:
                logger.warning(
                    f"{prefix}Saved gateway port {saved_port} is in use, using new port {gateway_port}"
                )
            if gateway_port != saved_port:
                # saved_port 为 None 时同样是新分配的端口，必须一并落库，否则每次
                # 恢复都会再换一个端口，且 metadata 里永远没有可用的端口键。
                self._save_gateway_port(
                    agent_id=agent_id,
                    gateway_port=gateway_port,
                    adapter_type=agent.adapter_type,
                )
            logger.info(f"{prefix}Reusing live sandbox: base_url=%s", base_url)
            return gateway_port

        if base_url:
            logger.warning(
                f"{prefix}Sandbox is not reachable, restarting it: base_url=%s",
                base_url,
            )

        return await self._restart_sandbox(agent)

    async def _resume_from_paused(self, agent_id: str) -> AgentRecord:
        """从 paused 状态恢复"""
        agent = self._get_agent(agent_id)
        prefix = _log_prefix(agent_id=agent_id)
        logger.info(f"{prefix}Resuming agent from paused state")

        # 1. 确保沙箱在跑：活着就复用，已经没了就重建（不再空等 60 秒后必然失败）
        gateway_port = await self._ensure_sandbox_ready_for_resume(agent)

        # 2. 调用 /agent/start（增加超时时间到180秒，因为可能需要加载模型）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        remote_runtime_agent_id: str | None = None
        try:
            start_payload = self._build_agent_start_payload(
                adapter_type=agent.adapter_type,
                model_id=agent.model_id,
                agent_key=agent.id,
                gateway_port=gateway_port,
            )
            logger.debug(
                f"{prefix}/agent/start payload for resume from paused: %s",
                redact_start_payload(start_payload),
            )
            try:
                started_agent = await adaptor_client.post(
                    "/agent/start", json=start_payload, timeout=180.0
                )
                remote_runtime_agent_id = started_agent.get("id")
                if (
                    not isinstance(remote_runtime_agent_id, str)
                    or not remote_runtime_agent_id
                ):
                    raise DomainError(
                        code=RUNTIME_START_FAILED,
                        message="Started agent response missing runtime agent id during resume from paused.",
                        details={"agent_id": agent_id},
                    )
                logger.info(
                    f"{prefix}/agent/start succeeded: runtime_agent_id=%s",
                    remote_runtime_agent_id,
                )
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agent/start failed with HTTP error: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to start runtime during resume from paused.",
                    details=self._describe_http_error(agent_id=agent_id, exc=exc),
                ) from exc
            except httpx.ReadTimeout as exc:
                logger.error(f"{prefix}/agent/start timeout: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Runtime start timed out during resume from paused.",
                    details={"agent_id": agent_id, "error": "ReadTimeout"},
                ) from exc
            except httpx.ConnectError as exc:
                logger.error(f"{prefix}/agent/start connection failed: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to connect to runtime during resume from paused.",
                    details={"agent_id": agent_id, "error": "ConnectError"},
                ) from exc
        finally:
            await adaptor_client.close()

        # 4. 恢复历史 session（witty-agent-server 可能因暂停期间重启导致 session 丢失）
        historical_sessions = self._session_manager.list_sessions(agent_id)
        if historical_sessions:
            adaptor_client = self._get_adaptor_http_client(agent_id)
            try:
                restored_count = 0
                failed_count = 0
                for session in historical_sessions:
                    try:
                        await adaptor_client.post(
                            f"/agents/{remote_runtime_agent_id}/sessions",
                            json={"session_id": session.id, "restore": True},
                            timeout=30.0,
                        )
                        restored_count += 1
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 409:
                            restored_count += 1
                        else:
                            failed_count += 1
                            logger.warning(
                                f"{prefix}Failed to restore historical session "
                                f"during resume from paused: session_id=%s status=%s",
                                session.id,
                                exc.response.status_code,
                            )
                if restored_count > 0 or failed_count > 0:
                    logger.info(
                        f"{prefix}Historical sessions restored during resume from paused: "
                        f"restored=%s failed=%s total=%s",
                        restored_count,
                        failed_count,
                        len(historical_sessions),
                    )
            finally:
                await adaptor_client.close()

        # 5. 更新状态
        return self._repository.update_agent_status(agent_id, AgentStatus.running)

    async def resume_agent(self, agent_id: str) -> AgentRecord:
        agent = self._get_agent(agent_id)

        if agent.status == AgentStatus.paused:
            self._ensure_transition(agent, AgentStatus.running)
            return await self._resume_from_paused(agent_id)
        elif agent.status == AgentStatus.deleted:
            return await self._resume_from_deleted(agent_id)
        elif agent.status == AgentStatus.error:
            # 状态机本来就允许 error → running（domain/enums.py），但原实现没有这个
            # 分支：恢复失败一次就被永久锁死——既不再被启动恢复挑中（只挑 running），
            # 前端「启动」按钮也必然失败。error 与 running 走同一条重建路径。
            self._ensure_transition(agent, AgentStatus.running)
            return await self._resume_from_running(agent_id)
        elif agent.status == AgentStatus.running:
            return await self._resume_from_running(agent_id)
        else:
            raise DomainError(
                code=INVALID_AGENT_TRANSITION,
                message="Cannot resume from current status.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

    async def _resume_from_running(self, agent_id: str) -> AgentRecord:
        """从 running 状态恢复（用于服务重启时，子进程已停止但数据库状态仍为 running）"""
        agent = self._get_agent(agent_id)
        prefix = _log_prefix(agent_id=agent_id)
        logger.info(
            f"{prefix}Resuming agent from running state (service restart recovery)"
        )

        # 获取隔离参数，并按「端口优先复用」重建沙箱
        profile_name = agent_id
        gateway_port = await self._restart_sandbox(agent)

        # 调用 /agent/start（增加超时时间到180秒，因为可能需要加载模型）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        remote_runtime_agent_id: str | None = None
        try:
            start_payload = self._build_agent_start_payload(
                adapter_type=agent.adapter_type,
                model_id=agent.model_id,
                agent_key=profile_name,
                gateway_port=gateway_port,
            )
            logger.debug(
                f"{prefix}/agent/start payload for recovery: %s",
                redact_start_payload(start_payload),
            )
            try:
                started_agent = await adaptor_client.post(
                    "/agent/start", json=start_payload, timeout=180.0
                )
                remote_runtime_agent_id = started_agent.get("id")
                if (
                    not isinstance(remote_runtime_agent_id, str)
                    or not remote_runtime_agent_id
                ):
                    raise DomainError(
                        code=RUNTIME_START_FAILED,
                        message="Started agent response missing runtime agent id during recovery.",
                        details={"agent_id": agent_id},
                    )
                logger.info(
                    f"{prefix}/agent/start succeeded: runtime_agent_id=%s",
                    remote_runtime_agent_id,
                )
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agent/start failed with HTTP error: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to start runtime during recovery.",
                    details=self._describe_http_error(agent_id=agent_id, exc=exc),
                ) from exc
            except httpx.ReadTimeout as exc:
                logger.error(f"{prefix}/agent/start timeout: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Runtime start timed out during recovery.",
                    details={"agent_id": agent_id, "error": "ReadTimeout"},
                ) from exc
            except httpx.ConnectError as exc:
                logger.error(f"{prefix}/agent/start connection failed: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to connect to runtime during recovery.",
                    details={"agent_id": agent_id, "error": "ConnectError"},
                ) from exc
        finally:
            await adaptor_client.close()

        # 5. 恢复历史 session（服务重启后 witty-agent-server 的 in-memory 存储已清空）
        historical_sessions = self._session_manager.list_sessions(agent_id)
        if historical_sessions:
            adaptor_client = self._get_adaptor_http_client(agent_id)
            try:
                restored_count = 0
                failed_count = 0
                for session in historical_sessions:
                    try:
                        await adaptor_client.post(
                            f"/agents/{remote_runtime_agent_id}/sessions",
                            json={"session_id": session.id, "restore": True},
                            timeout=30.0,
                        )
                        restored_count += 1
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 409:
                            # session 已存在（理论上不会，防御性处理）
                            restored_count += 1
                        else:
                            failed_count += 1
                            logger.warning(
                                f"{prefix}Failed to restore historical session: "
                                f"session_id=%s status=%s",
                                session.id,
                                exc.response.status_code,
                            )
                if restored_count > 0 or failed_count > 0:
                    logger.info(
                        f"{prefix}Historical sessions restored: "
                        f"restored=%s failed=%s total=%s",
                        restored_count,
                        failed_count,
                        len(historical_sessions),
                    )
            finally:
                await adaptor_client.close()

        # 6. 更新状态（保持 running 状态，但更新时间戳）
        return self._repository.update_agent_status(agent_id, AgentStatus.running)

    async def send_message(
        self,
        agent_id: str,
        session_id: str,
        content: str,
        adaptor_client: AdaptorHttpClient | None = None,
    ) -> dict[str, Any]:
        self._logger.info(
            f"send_message called: agent_id={agent_id}, session_id={session_id}"
        )
        agent = self._get_agent(agent_id)
        # 归属校验只做一次，读到的 session 沿调用链传下去
        # （_prepare_ws_message_client / _auto_generate_session_title 都要用）。
        session = self._session_manager.get_session(agent_id, session_id)

        if adaptor_client is None:
            adaptor_client = self._get_adaptor_http_client(agent_id)
            client_closer = adaptor_client.close
        else:

            async def client_closer() -> None:
                return None

        if agent.status is AgentStatus.paused:
            agent = await self.resume_agent(agent_id)
        elif agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to send messages.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        self._logger.info(
            f"Agent status OK: {agent.status}, preparing WebSocket client"
        )

        self._repository.create_message(
            agent_id=agent_id,
            session_id=session_id,
            role="user",
            content=content,
        )
        self._auto_generate_session_title(agent_id, session_id, session=session)

        ws_content = self._maybe_prepend_interruption_prefix(session_id, content)
        ws_client = await self._prepare_ws_message_client(
            agent_id, session_id, ws_content, agent=agent, session=session
        )

        self._logger.info(
            "WebSocket client ready: ws_client_id=%s is_connected=%s agent_id=%s session_id=%s",
            id(ws_client),
            ws_client.is_connected,
            agent_id,
            session_id,
        )

        events: list[dict[str, Any]] = []
        has_completed = False
        try:
            async for event in ws_client.recv():
                event_dict = dict(event)
                self._sync_runtime_session_identity_from_event(
                    agent_id=agent_id,
                    session_id=session_id,
                    event=event_dict,
                )
                # 刷新session状态
                self._sync_session_state_from_event(
                    agent_id=agent_id,
                    session_id=session_id,
                    event=event_dict,
                )

                # Handle client.error events from witty-agent-server
                if event_dict["type"] in {"client.error", "stream.error"}:
                    error_payload = event_dict.get("payload", {})
                    error_code = error_payload.get("code", "UNKNOWN_ERROR")
                    error_message = error_payload.get(
                        "message", "Unknown error from adaptor"
                    )
                    raise DomainError(
                        code=error_code,
                        message=error_message,
                        details={"session_id": session_id, "agent_id": agent_id},
                    )
                if self._should_filter_session_event(event_dict):
                    self._logger.info(
                        "filtered session state event from response: agent_id=%s session_id=%s event_type=%s",
                        agent_id,
                        session_id,
                        event_dict["type"],
                    )
                    continue
                normalized_event = normalize_artifact_event(
                    event_dict, workspace_path=agent.workspace_path
                )
                if normalized_event is None:
                    self._logger.info(
                        "dropped artifact event outside workspace: agent_id=%s session_id=%s event_type=%s",
                        agent_id,
                        session_id,
                        event_dict["type"],
                    )
                    continue
                event_dict = normalized_event
                self._logger.info(
                    f"received event: {json.dumps(event_dict, indent=2, ensure_ascii=False)}"
                )
                events.append(event_dict)
                if event_dict["type"] in {"message.completed", "turn.completed"}:
                    has_completed = True
                    self._logger.info("message.completed received, stopping")
                    break
        except Exception:
            self._session_manager.upsert_session(
                session_id=session_id,
                agent_id=agent_id,
                status="error",
            )
            raise
        finally:
            await self._close_ws_message_client(
                agent_id=agent_id,
                session_id=session_id,
                ws_client=ws_client,
            )
            await client_closer()

        if not has_completed:
            raise DomainError(
                code="INVALID_MESSAGE_STREAM",
                message="Message stream terminated before completion event.",
                details={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "events_count": len(events),
                    "last_event_type": events[-1].get("type") if events else None,
                },
            )
        return {
            "sandbox_type": agent.sandbox_type,
            "events": events,
        }

    async def send_message_stream(
        self,
        agent_id: str,
        session_id: str,
        content: str,
    ) -> AsyncIterator[dict[str, Any]]:
        agent = self._get_agent(agent_id)
        # 同 send_message：只查一次，读完沿调用链传下去。
        session = self._session_manager.get_session(agent_id, session_id)

        if agent.status is AgentStatus.paused:
            agent = await self.resume_agent(agent_id)
        elif agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to send messages.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        self._repository.create_message(
            agent_id=agent_id,
            session_id=session_id,
            role="user",
            content=content,
        )
        self._auto_generate_session_title(agent_id, session_id, session=session)

        ws_content = self._maybe_prepend_interruption_prefix(session_id, content)
        ws_client = await self._prepare_ws_message_client(
            agent_id, session_id, ws_content, agent=agent, session=session
        )

        # Start a new stream generation
        stream_gen = _stream_registry.start_stream(session_id)
        queue = _stream_registry.subscribe(session_id)

        seq_no = 0
        assistant_text = ""
        assistant_msg_id: str | None = None
        terminal_received = False
        tokens_since_checkpoint = 0
        last_checkpoint_time = time.monotonic()
        TOKENS_PER_CHECKPOINT = 100
        CHECKPOINT_INTERVAL_S = 2.0
        sandbox_type = agent.sandbox_type

        async def consume_ws() -> None:
            """后台任务：消费 WebSocket 事件，持久化，并推送到stream_registry"""
            nonlocal seq_no, assistant_text, assistant_msg_id, terminal_received
            nonlocal tokens_since_checkpoint, last_checkpoint_time

            # 落库缓冲：窗口内的事件合并成一次事务（见 PERSIST_BATCH_* 说明）。
            pending_events: list[tuple[int, str, dict[str, Any]]] = []
            last_flush_time = time.monotonic()
            # 非正常结束时的消息终态；None 表示本轮正常结束（已完成/被前端中断）。
            failure_status: MessageStatus | None = None

            def flush_events() -> None:
                """把缓冲的事件合并成一次提交落库。"""
                nonlocal pending_events, last_flush_time
                if not pending_events or assistant_msg_id is None:
                    return
                batch, pending_events = pending_events, []
                last_flush_time = time.monotonic()
                try:
                    self._repository.create_message_events_bulk(
                        agent_id=agent_id,
                        session_id=session_id,
                        events=batch,
                        message_id=assistant_msg_id,
                    )
                except Exception:
                    self._logger.warning(
                        "Failed to persist %d events: agent_id=%s session_id=%s",
                        len(batch),
                        agent_id,
                        session_id,
                        exc_info=True,
                    )

            def finalize_abnormal_turn(status: MessageStatus) -> None:
                """流被异常中断时的收尾：落库 + 正文 + 终态 + 会话状态复位。

                不做这件事的后果就是生产上看到的现象：assistant 消息永远停在
                ``generating``、session 永远停在 ``running``；前端重启后按
                ``generating`` 判定"生成中"并去重连，而本地已没有活动流，
                于是永久卡在"生成回复中"且正文被清空。
                """
                flush_events()
                if assistant_msg_id is not None:
                    try:
                        self._repository.finalize_message(
                            assistant_msg_id,
                            status=status,
                            content=assistant_text,
                            last_stream_at=datetime.now(UTC),
                        )
                    except Exception:
                        self._logger.warning(
                            "Failed to finalize interrupted message: msg_id=%s",
                            assistant_msg_id,
                            exc_info=True,
                        )
                try:
                    session_record = self._session_manager.get_session(
                        agent_id, session_id
                    )
                    if (
                        session_record is not None
                        and session_record.status == "running"
                    ):
                        self._session_manager.upsert_session(
                            session_id=session_id,
                            agent_id=agent_id,
                            status="idle",
                        )
                except Exception:
                    self._logger.warning(
                        "Failed to reset session state after abnormal stream end: "
                        "agent_id=%s session_id=%s",
                        agent_id,
                        session_id,
                        exc_info=True,
                    )

            try:
                async for event in ws_client.recv():
                    event_dict = dict(event)
                    self._sync_runtime_session_identity_from_event(
                        agent_id=agent_id,
                        session_id=session_id,
                        event=event_dict,
                    )
                    self._sync_session_state_from_event(
                        agent_id=agent_id,
                        session_id=session_id,
                        event=event_dict,
                    )

                    if event_dict["type"] in {"client.error", "stream.error"}:
                        error_payload = event_dict.get("payload", {})
                        error_code = error_payload.get("code", "UNKNOWN_ERROR")
                        error_message = error_payload.get(
                            "message", "Unknown error from adaptor"
                        )
                        failure_status = MessageStatus.error
                        self._logger.error(
                            "Stream error in background consumer: agent_id=%s session_id=%s code=%s message=%s",
                            agent_id,
                            session_id,
                            error_code,
                            error_message,
                        )
                        _stream_registry.push_event(session_id, event_dict, stream_gen)
                        _stream_registry.end_stream(session_id)
                        return

                    if self._should_filter_session_event(event_dict):
                        self._logger.info(
                            "filtered session state event from stream: agent_id=%s session_id=%s event_type=%s",
                            agent_id,
                            session_id,
                            event_dict["type"],
                        )
                        continue

                    normalized_event = normalize_artifact_event(
                        event_dict, workspace_path=agent.workspace_path
                    )
                    if normalized_event is None:
                        self._logger.info(
                            "dropped artifact event outside workspace: agent_id=%s session_id=%s event_type=%s",
                            agent_id,
                            session_id,
                            event_dict["type"],
                        )
                        continue
                    event_dict = normalized_event

                    if assistant_msg_id is None:
                        assistant_msg_id = self._repository.create_message(
                            agent_id=agent_id,
                            session_id=session_id,
                            role="assistant",
                            content="",
                            status=MessageStatus.generating,
                        )

                    event_type = event_dict["type"]
                    payload = (
                        event_dict.get("payload")
                        if isinstance(event_dict.get("payload"), dict)
                        else {}
                    )
                    if event_type in TRANSIENT_EVENT_TYPES:
                        # 仍会走到下面的 push_event，只是不落库。
                        self._logger.debug(
                            "skip persisting transient event: agent_id=%s session_id=%s event_type=%s",
                            agent_id,
                            session_id,
                            event_type,
                        )
                    else:
                        seq_no += 1
                        pending_events.append((seq_no, event_type, payload))
                        if (
                            len(pending_events) >= PERSIST_BATCH_MAX_EVENTS
                            or time.monotonic() - last_flush_time
                            >= PERSIST_BATCH_INTERVAL_S
                        ):
                            flush_events()

                    if event_type == "message.delta":
                        delta = payload.get("delta", "")
                        assistant_text += delta
                        tokens_since_checkpoint += len(delta) // 4
                    elif event_type == "message.completed":
                        completed_text = payload.get("text", "")
                        if completed_text:
                            assistant_text = completed_text

                    now = time.monotonic()
                    if assistant_msg_id and (
                        tokens_since_checkpoint >= TOKENS_PER_CHECKPOINT
                        or now - last_checkpoint_time >= CHECKPOINT_INTERVAL_S
                        or event_type in {"message.completed", "turn.completed"}
                    ):
                        if assistant_text:
                            try:
                                self._repository.update_message_content(
                                    assistant_msg_id, assistant_text
                                )
                                self._repository.update_message_stream_at(
                                    assistant_msg_id
                                )
                            except Exception:
                                self._logger.warning(
                                    "Failed to update message content checkpoint: msg_id=%s",
                                    assistant_msg_id,
                                    exc_info=True,
                                )
                        tokens_since_checkpoint = 0
                        last_checkpoint_time = now

                    if event_type in {"message.completed", "turn.completed"}:
                        terminal_received = True
                        if assistant_msg_id:
                            flush_events()
                            try:
                                self._repository.update_message_status(
                                    assistant_msg_id, MessageStatus.completed
                                )
                                self._logger.info(
                                    "update_message_status in ws: assistant_msg_id=%s state=%s",
                                    assistant_msg_id,
                                    MessageStatus.completed,
                                )
                            except Exception:
                                self._logger.warning(
                                    "Failed to update message status: msg_id=%s",
                                    assistant_msg_id,
                                    exc_info=True,
                                )
                            try:
                                self._repository.compact_message_delta_events(
                                    assistant_msg_id
                                )
                            except Exception:
                                self._logger.warning(
                                    "Failed to compact delta events: msg_id=%s",
                                    assistant_msg_id,
                                    exc_info=True,
                                )

                    _stream_registry.push_event(session_id, event_dict, stream_gen)

                    # 必须显式让出事件循环：websockets 已经把帧收进内存队列时，
                    # ``async for`` 不会真正挂起，整个积压会在"不回到事件循环"的
                    # 一次连续执行里处理完，期间的 keepalive ping 得不到 pong。
                    await asyncio.sleep(0)

                    if event_type in {"message.completed", "turn.completed"}:
                        break
            except Exception:
                failure_status = MessageStatus.interrupted
                self._logger.warning(
                    "Background WS consumer error: agent_id=%s session_id=%s",
                    agent_id,
                    session_id,
                    exc_info=True,
                )
                _stream_registry.push_event(
                    session_id,
                    {
                        "type": "stream.error",
                        "payload": {
                            "code": "CONSUMER_ERROR",
                            "message": "Background WS consumer encountered an error",
                        },
                    },
                    stream_gen,
                )
            finally:
                if failure_status is not None:
                    finalize_abnormal_turn(failure_status)
                _stream_registry.end_stream(session_id)
                _stream_registry.cleanup(session_id)
                await self._close_ws_message_client(
                    agent_id=agent_id,
                    session_id=session_id,
                    ws_client=ws_client,
                )

        bg_task = asyncio.create_task(consume_ws())
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)

        try:
            while True:
                event_dict = await queue.get()
                if event_dict is None:  # sentinel — stream ended
                    break
                yield {
                    "sandbox_type": sandbox_type,
                    "event": event_dict,
                }
        except GeneratorExit:
            self._logger.info(
                "SSE client disconnected: agent_id=%s session_id=%s — background consumer continues",
                agent_id,
                session_id,
            )
            _stream_registry.unsubscribe(session_id, queue)
            raise
        except asyncio.CancelledError:
            self._logger.info(
                "SSE stream cancelled: agent_id=%s session_id=%s — background consumer continues",
                agent_id,
                session_id,
            )
            _stream_registry.unsubscribe(session_id, queue)
            raise
        except Exception:
            self._logger.exception(
                "SSE stream error: agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
            _stream_registry.unsubscribe(session_id, queue)
            raise

    async def reconnect_stream(
        self,
        agent_id: str,
        session_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        agent = self._get_agent(agent_id)
        # _stream_registry 是进程级共享的，按 session_id 取缓冲/订阅：
        # 不校验归属就等于允许用 A 的 agent_id 读 B 会话的增量内容。
        self._session_manager.get_session(agent_id, session_id)
        sandbox_type = agent.sandbox_type

        if not _stream_registry.is_active(session_id):
            self._logger.info(
                "reconnect_stream: no active stream for session_id=%s",
                session_id,
            )
            return

        # Replay buffered events
        buffered = _stream_registry.get_buffered_events(session_id)
        self._logger.info(
            "reconnect_stream: replaying %d buffered events for session_id=%s",
            len(buffered),
            session_id,
        )
        for event_dict in buffered:
            yield {
                "sandbox_type": sandbox_type,
                "event": event_dict,
            }

        # Subscribe to live events
        queue = _stream_registry.subscribe(session_id)
        try:
            while True:
                event_dict = await queue.get()
                if event_dict is None:
                    break
                yield {
                    "sandbox_type": sandbox_type,
                    "event": event_dict,
                }
        except GeneratorExit:
            _stream_registry.unsubscribe(session_id, queue)
            raise
        except asyncio.CancelledError:
            _stream_registry.unsubscribe(session_id, queue)
            raise
        except Exception:
            _stream_registry.unsubscribe(session_id, queue)
            raise

    async def _handle_user_abort(
        self,
        ws_client: WebSocketClient,
        agent_id: str,
        session_id: str,
    ) -> None:
        await ws_client.send({"type": "message.abort", "payload": {}})
        self._logger.info(
            "Sending message.abort via WS: agent_id=%s session_id=%s",
            agent_id,
            session_id,
        )

    async def answer_question(
        self,
        *,
        agent_id: str,
        session_id: str,
        request_id: str,
        answers: list[list[str]],
    ) -> None:
        """通过现有 WS 连接向 agent server 发送 question.reply 消息。"""
        ws_client = self._get_active_ws_client(agent_id, session_id)
        await ws_client.send(
            {
                "type": "question.reply",
                "payload": {
                    "request_id": request_id,
                    "answers": answers,
                },
            }
        )
        self._logger.info(
            "answer_question sent: agent_id=%s session_id=%s request_id=%s",
            agent_id,
            session_id,
            request_id,
        )

    async def reject_question(
        self,
        *,
        agent_id: str,
        session_id: str,
        request_id: str,
    ) -> None:
        """通过现有 WS 连接向 agent server 发送 question.reject 消息。"""
        ws_client = self._get_active_ws_client(agent_id, session_id)
        await ws_client.send(
            {
                "type": "question.reject",
                "payload": {
                    "request_id": request_id,
                },
            }
        )
        self._logger.info(
            "reject_question sent: agent_id=%s session_id=%s request_id=%s",
            agent_id,
            session_id,
            request_id,
        )

    def _get_active_ws_client(
        self,
        agent_id: str,
        session_id: str,
        *,
        agent: AgentRecord | None = None,
        session: SessionRecord | None = None,
    ) -> WebSocketClient:
        """获取当前 session 的活动 WS 客户端。"""
        endpoint = self._get_adaptor_endpoint(
            agent_id, session_id, agent=agent, session=session
        )
        ws_client = self._ws_client_pool.get_client(
            agent_id=agent_id,
            endpoint=endpoint,
            factory=lambda url: WebSocketClient(base_url=url),
        )
        if not ws_client.is_connected:
            raise DomainError(
                code="WS_NOT_CONNECTED",
                message="WebSocket is not connected for this session.",
                details={"agent_id": agent_id, "session_id": session_id},
            )
        return ws_client

    async def create_session(
        self,
        agent_id: str,
        runtime_agent_id: str | None = None,
    ) -> SessionRecord:
        agent = self._get_agent(agent_id)
        if agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to create session.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            session = await self._session_manager.create_session_remote(
                agent_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        finally:
            await adaptor_client.close()

        return session

    async def list_sessions(
        self,
        agent_id: str,
        runtime_agent_id: str | None = None,
    ) -> list[SessionRecord]:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            return await self._session_manager.list_sessions_remote(
                agent_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        finally:
            await adaptor_client.close()

    async def get_session(
        self,
        agent_id: str,
        session_id: str,
        runtime_agent_id: str | None = None,
    ) -> SessionRecord:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            return await self._session_manager.get_session_remote(
                agent_id,
                session_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        finally:
            await adaptor_client.close()

    async def get_session_events(
        self,
        agent_id: str,
        session_id: str,
        offset: int = 0,
        limit: int = 50,
        runtime_agent_id: str | None = None,
    ) -> dict[str, Any]:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            session = self._session_manager.get_session(agent_id, session_id)
            resolved_runtime_agent_id = session.remote_runtime_agent_id
            if resolved_runtime_agent_id is None:
                resolved_runtime_agent_id = (
                    await self._session_manager.resolve_runtime_agent_id(
                        adaptor_client=adaptor_client,
                        runtime_agent_id=runtime_agent_id,
                    )
                )
            return await adaptor_client.get(
                f"/agents/{resolved_runtime_agent_id}/sessions/{session_id}/events",
                params={"offset": offset, "limit": limit},
            )
        finally:
            await adaptor_client.close()

    async def delete_session(
        self,
        agent_id: str,
        session_id: str,
        runtime_agent_id: str | None = None,
    ) -> None:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            await self._session_manager.delete_session_remote(
                agent_id,
                session_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        finally:
            await adaptor_client.close()

    async def abort_session(
        self,
        agent_id: str,
        session_id: str,
        runtime_agent_id: str | None = None,
    ) -> dict[str, object]:
        # 与 send_message 一致：先校验会话归属，再做本地副作用。下面的
        # find_last_assistant_message_for_session 与关 WS 都按 session_id 操作，
        # 不校验就等于允许用 A 的 agent_id 把 B 会话的消息改成 interrupted。
        # 读到的 session 传给 _get_active_ws_client，避免再查一次。
        session = self._session_manager.get_session(agent_id, session_id)

        adaptor_client = self._get_adaptor_http_client(agent_id)
        remote_error: Exception | None = None
        try:
            await self._session_manager.abort_session_remote(
                agent_id,
                session_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        except Exception as exc:
            # adaptor 不可达等失败不应阻塞后续本地副作用：调度器 _run_agent_turn
            # 仍依赖 message.status=interrupted + WS 关闭来识别 abort 并落终态，
            # 否则定时 run 会永远卡在 running、任务下次到点直接 skipped。
            remote_error = exc
        finally:
            await adaptor_client.close()

        last_msg = self._repository.find_last_assistant_message_for_session(session_id)
        if last_msg is not None:
            try:
                self._repository.update_message_status(
                    last_msg.id, MessageStatus.interrupted
                )
                self._logger.info(
                    "update_message_status in abort: msg_id=%s state=%s",
                    last_msg.id,
                    MessageStatus.interrupted,
                )
            except Exception:
                self._logger.warning(
                    "Failed to mark message interrupted after abort: msg_id=%s",
                    last_msg.id,
                    exc_info=True,
                )

        # 关闭当前会话的活动消息 WS：调度 run 的 _run_agent_turn 正在消费
        # manager.send_message_stream，若不主动断开，它会继续等待 WS 事件，
        # 永远感知不到用户已经 abort。关闭后流会立即结束并在调度服务中落终态。
        try:
            ws_client = self._get_active_ws_client(
                agent_id, session_id, session=session
            )
        except DomainError:
            ws_client = None
        if ws_client is not None:
            await self._close_ws_message_client(
                agent_id=agent_id,
                session_id=session_id,
                ws_client=ws_client,
            )

        # 远端 abort 失败时向上层返回错误，但本地副作用已全部执行，
        # 不会让定时 run 卡在 running 状态。
        if remote_error is not None:
            raise remote_error

        return {"id": session_id, "aborted": True}

    async def delete_agent(self, agent_id: str) -> None:
        agent = self._get_agent(agent_id)
        sandbox_state = self._repository.get_sandbox_state(agent_id)

        cleanup_errors: list[dict[str, str]] = []

        # 1. 备份运行时（暂注释，避免占用空间）
        # if sandbox_state is not None:
        #     self._collect_error(
        #         cleanup_errors,
        #         "runtime_backup",
        #         lambda: self._backup_runtime(agent_id, agent.adapter_type),
        #     )

        # 2. 停止运行时
        if agent.status in {AgentStatus.running, AgentStatus.paused}:
            try:
                await self._stop_runtime(agent_id)
            except Exception as exc:
                cleanup_errors.append(self._cleanup_error("runtime_stop", exc))

        # 3. 清理沙箱
        if sandbox_state is not None:
            try:
                self._cleanup_sandbox(agent_id)
            except Exception as exc:
                cleanup_errors.append(self._cleanup_error("sandbox_cleanup", exc))

        # 4. 前置阶段失败即中止
        blocking_errors = [
            error
            for error in cleanup_errors
            if not self._is_tolerable_cleanup_error(error)
        ]
        if blocking_errors:
            self._raise_operation_failed(
                code=AGENT_DELETE_FAILED,
                message="Agent delete failed.",
                agent_id=agent_id,
                cause=RuntimeError(blocking_errors[0]["error"]),
                cleanup_errors=cleanup_errors,
            )
        if cleanup_errors:
            logger.warning(
                "[AgentManager] Ignoring tolerable cleanup errors: agent_id=%s errors=%s",
                agent_id,
                cleanup_errors,
            )

        # 5. 不可逆阶段（放在最后）：更新状态并删除 agent 记录。
        try:
            self._repository.update_agent_status(agent_id, AgentStatus.deleted)
            logger.info("[AgentManager] Agent status updated to deleted in database")
            # 彻底删除 agent 记录（包括关联的 session、message、skill 等）
            self._repository.delete_agent(agent_id)
        except Exception as exc:
            cleanup_errors.append(self._cleanup_error("agent_delete", exc))
            logger.error("[AgentManager] Failed to delete agent record: %s", exc)
            self._raise_operation_failed(
                code=AGENT_DELETE_FAILED,
                message="Agent delete failed.",
                agent_id=agent_id,
                cause=exc,
                cleanup_errors=cleanup_errors,
            )

    @staticmethod
    def _is_tolerable_cleanup_error(error: dict[str, str]) -> bool:
        """沙箱句柄已不存在属于"已经清理过"，不算删除失败。

        按错误码判定：沙箱 backend 在句柄丢失时抛 SANDBOX_NOT_FOUND
        （sandbox/base.py 的 sandbox_not_found）。匹配 message 文案会在上游改一句话
        之后静默失效，也会放过恰好带上同一句话的无关错误。
        """
        return (
            error.get("stage") == "sandbox_cleanup"
            and error.get("code") == SANDBOX_NOT_FOUND
        )

    def _create_agent_record(
        self,
        *,
        agent_id: str,
        request: AgentCreateRequest,
        workspace_path: str,
    ) -> AgentRecord:
        return self._repository.create_agent_with_id(
            agent_id=agent_id,
            name=request.name,
            description=request.description,
            sandbox_type=request.sandbox_type,
            adapter_type=request.adapter_type,
            workspace_path=workspace_path,
            idle_timeout_seconds=request.idle_timeout_seconds,
            status=AgentStatus.creating,
            sandbox_id=request.sandbox_id,
            model_id=request.model_id,
            mcp_server_list=request.mcp_server_list,
        )

    def _maybe_prepend_interruption_prefix(self, session_id: str, content: str) -> str:
        if self._repository.get_last_assistant_status(session_id) == "interrupted":
            self._logger.info(
                "Last assistant message was interrupted, prepending interruption prefix: session_id=%s",
                session_id,
            )
            return INTERRUPTION_PREFIX + content
        return content

    def _get_agent(self, agent_id: str) -> AgentRecord:
        agent = self._repository.get_agent(agent_id)
        if agent is None:
            raise agent_not_found(agent_id=agent_id)
        return agent

    def _get_adaptor_endpoint(
        self,
        agent_id: str,
        session_id: str,
        *,
        agent: AgentRecord | None = None,
        session: SessionRecord | None = None,
    ) -> AdaptorEndpoint:
        """组装 adaptor WS endpoint。

        agent / session 由调用方传入时跳过重复点查：一次请求里这两行通常已经被入口
        读过，而本仓库每次 repository 调用都要新建 ORM Session（实测约 0.23ms）。
        """
        sandbox_state = self._get_sandbox_state(agent_id)
        session_record = session or self._session_manager.get_session(
            agent_id, session_id
        )
        if session_record.remote_runtime_agent_id is None:
            raise DomainError(
                code="RUNTIME_AGENT_ID_MISSING",
                message="Remote runtime agent id was not found for session.",
                details={"agent_id": agent_id, "session_id": session_id},
            )
        base_url = sandbox_state.adapter_base_url
        if base_url.startswith("https"):
            scheme = "wss"
        elif base_url.startswith("http"):
            scheme = "ws"
        else:
            scheme = "ws"
        host = base_url.split("://")[-1]
        ws_base_url = (
            f"{scheme}://{host}/agents/{session_record.remote_runtime_agent_id}"
        )
        return AdaptorEndpoint(
            base_url=ws_base_url,
            session_id=session_id,
            sandbox_type=(agent or self._get_agent(agent_id)).sandbox_type,
        )

    async def _prepare_ws_message_client(
        self,
        agent_id: str,
        session_id: str,
        content: str,
        *,
        agent: AgentRecord | None = None,
        session: SessionRecord | None = None,
    ) -> WebSocketClient:
        endpoint = self._get_adaptor_endpoint(
            agent_id, session_id, agent=agent, session=session
        )
        self._logger.info(
            f"_prepare_ws: agent_id={agent_id}, session_id={session_id}, endpoint={endpoint}"
        )
        ws_client = self._ws_client_pool.get_client(
            agent_id=agent_id,
            endpoint=endpoint,
            factory=lambda url: WebSocketClient(base_url=url),
        )

        self._logger.info(
            "_prepare_ws: pool returned client: ws_client_id=%s is_connected=%s agent_id=%s session_id=%s",
            id(ws_client),
            ws_client.is_connected,
            agent_id,
            session_id,
        )

        if not ws_client.is_connected:
            self._logger.info(
                f"_prepare_ws: connecting to {endpoint.base_url}/sessions/{session_id}/ws"
            )
            await ws_client.connect(session_id)
            self._logger.info("_prepare_ws: connected successfully")

        msg: OutboundMessage = {
            "type": "message.create",
            "payload": {"message": content},
        }
        self._logger.info(f"_prepare_ws: sending message: {msg}")
        await ws_client.send(msg)
        self._logger.info("_prepare_ws: message sent")
        return ws_client

    async def _close_ws_message_client(
        self,
        *,
        agent_id: str,
        session_id: str,
        ws_client: WebSocketClient,
    ) -> None:
        """Close per-turn websocket so unread runtime events cannot leak into later turns."""
        close = getattr(ws_client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:
                self._logger.warning(
                    "failed to close ws message client: agent_id=%s session_id=%s error=%s",
                    agent_id,
                    session_id,
                    exc,
                )
        self._ws_client_pool.remove_client(agent_id, session_id)

    def _sync_session_state_from_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        """根据 adaptor WS 事件刷新本地 session 状态。"""
        event_type = event.get("type")
        payload = event.get("payload")
        normalized_payload = payload if isinstance(payload, dict) else {}

        if event_type in {"session.state_changed", "session.heartbeat"}:
            state = normalized_payload.get("state")
            if isinstance(state, str) and state in {"running", "idle", "error"}:
                self._logger.info(
                    "sync session state from ws event: agent_id=%s session_id=%s event_type=%s state=%s",
                    agent_id,
                    session_id,
                    event_type,
                    state,
                )
                self._session_manager.upsert_session(
                    session_id=session_id,
                    agent_id=agent_id,
                    status=state,
                )
            return

        if event_type in {"message.completed", "turn.completed"}:
            self._logger.info(
                "sync session state from ws event: agent_id=%s session_id=%s event_type=%s state=idle",
                agent_id,
                session_id,
                event_type,
            )
            self._session_manager.upsert_session(
                session_id=session_id,
                agent_id=agent_id,
                status="idle",
            )
            return

        if event_type in {"client.error", "stream.error"}:
            self._logger.info(
                "sync session state from ws event: agent_id=%s session_id=%s event_type=%s state=error",
                agent_id,
                session_id,
                event_type,
            )
            self._session_manager.upsert_session(
                session_id=session_id,
                agent_id=agent_id,
                status="error",
            )

    def _auto_generate_session_title(
        self,
        agent_id: str,
        session_id: str,
        *,
        session: SessionRecord | None = None,
    ) -> None:
        """自动生成会话标题；调用方已读过 session 时直接传入，省一次点查。"""
        try:
            session_record = session or self._session_manager.get_session(
                agent_id, session_id
            )
            if session_record.title:
                return
            first_msg = self._repository.get_first_user_message(session_id)
            if first_msg:
                title = first_msg[:30].replace("\n", " ")
                self._repository.update_session_metadata(session_id, title=title)
        except Exception:
            self._logger.warning(
                "Failed to auto-generate session title: agent_id=%s session_id=%s",
                agent_id,
                session_id,
                exc_info=True,
            )

    def _sync_runtime_session_identity_from_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        if event.get("type") != "session.runtime.changed":
            return

        payload = event.get("payload")
        normalized_payload = payload if isinstance(payload, dict) else {}
        runtime_session_id = normalized_payload.get("runtime_session_id")
        runtime_session_key = normalized_payload.get("runtime_session_key")
        runtime_type = event.get("runtime_type")

        if not isinstance(runtime_session_id, str) or not runtime_session_id:
            self._logger.warning(
                "skip runtime session identity sync due to missing runtime_session_id: "
                "agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
            return
        if not isinstance(runtime_session_key, str) or not runtime_session_key:
            self._logger.warning(
                "skip runtime session identity sync due to missing runtime_session_key: "
                "agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
            return
        if not isinstance(runtime_type, str) or not runtime_type:
            self._logger.warning(
                "skip runtime session identity sync due to missing runtime_type: "
                "agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
            return

        try:
            self._session_manager.get_session(agent_id, session_id)
            self._session_manager.update_session_runtime_identity(
                session_id=session_id,
                runtime_type=runtime_type,
                runtime_session_id=runtime_session_id,
                runtime_session_key=runtime_session_key,
            )
        except DomainError as exc:
            self._logger.exception(
                "failed to sync runtime session identity: "
                "agent_id=%s session_id=%s runtime_type=%s runtime_session_id=%s error_code=%s",
                agent_id,
                session_id,
                runtime_type,
                runtime_session_id,
                exc.code,
            )

    def _should_filter_session_event(self, event: dict[str, Any]) -> bool:
        """过滤仅用于本地 session 状态同步的内部事件。"""
        event_type = event.get("type")
        return event_type in {
            "session.state_changed",
            "session.heartbeat",
            "session.runtime.changed",
        }

    def _get_sandbox_state(self, agent_id: str) -> SandboxState:
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is None:
            raise DomainError(
                code=SANDBOX_STATE_NOT_FOUND,
                message="Sandbox state was not found.",
                details={"agent_id": agent_id},
            )
        return sandbox_state

    def _get_adaptor_http_client(self, agent_id: str) -> AdaptorHttpClient:
        """获取到 witty-agent-server 的 HTTP 客户端"""
        sandbox_state = self._get_sandbox_state(agent_id)
        return AdaptorHttpClient(base_url=sandbox_state.adapter_base_url)

    def _check_and_update_agent_status_if_needed(self, agent_id: str) -> AgentRecord:
        """沙箱进程**确定**已经退出时，才把 agent 置为 error。

        旧实现用一次 HTTP 探活失败就翻状态，而那次探活是 30s 超时的默认客户端，跑在
        同步请求路径（`GET /agents` 等 sync handler + `run_until_complete`）上：一次网络
        抖动、一次 GC 停顿、事件循环一时繁忙，都会把健康的 agent 打成 `error`。再叠加
        「启动恢复只挑 running」，就是一次抖动 = 永久死亡。

        现在只认**确定性证据**：local_process 沙箱记录在句柄里的 pid 在 ``/proc`` 中
        已经不存在。判断不出来（老句柄没记 pid、环境没有 /proc）就保持现状——
        宁可漏报一个死掉的沙箱（恢复流程会处理），也不误判一个活着的。
        """
        agent = self._get_agent(agent_id)

        if agent.status is not AgentStatus.running:
            return agent

        # 只对 local_process 类型进行检查（docker 容器存活由 daemon 保证）
        if agent.sandbox_type != "local_process":
            return agent

        # 如果正在恢复中，跳过健康检查（避免恢复过程中状态被修改）
        if _recovery_lock.locked():
            self._logger.debug(
                "Skipping health check during recovery: agent_id=%s",
                agent_id,
            )
            return agent

        # 检查沙箱进程是否还在运行
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is None:
            return agent

        if not self._sandbox_process_is_gone(sandbox_state):
            return agent

        self._logger.warning(
            "Sandbox process is gone, marking agent as error: agent_id=%s pid=%s",
            agent_id,
            dict(sandbox_state.handle.metadata).get("pid"),
        )
        return self._repository.update_agent_status(agent_id, AgentStatus.error)

    @staticmethod
    def _sandbox_process_is_gone(sandbox_state: SandboxState) -> bool:
        """local_process 沙箱进程是否**确定**已经退出（僵尸也算退出）。"""
        if not Path("/proc").is_dir():
            # 没有 /proc（非 Linux）：判断不了，绝不能因此翻状态
            return False
        pid = dict(sandbox_state.handle.metadata).get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        stat = read_proc_stat(pid)
        if stat is None:
            # /proc 在，但这个 pid 不在 = 确定已退出
            return True
        return stat[0] == "Z"

    def _backup_runtime(
        self, agent_id: str, runtime_type: str = "openclaw"
    ) -> Path | None:
        """备份运行时文件"""
        backup_store = RuntimeBackupStore()
        try:
            return backup_store.backup(agent_id, runtime_type)
        except Exception:
            return None

    def _cleanup_sandbox(self, agent_id: str) -> None:
        """清理沙箱"""
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is not None:
            self._sandbox_backend.cleanup(sandbox_state.handle)

    async def _stop_runtime(self, agent_id: str) -> None:
        """停止 witty-agent-server 运行时"""
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            # 可能已经停止
            with contextlib.suppress(httpx.HTTPStatusError):
                await adaptor_client.post("/agent/stop", json={})
        finally:
            await adaptor_client.close()

    def _compensate_sandbox_state(
        self,
        *,
        agent_id: str,
        sandbox_handle: SandboxHandle,
        adapter_base_url: str,
        adapter_ready: bool,
        last_error: str,
        status_on_error: AgentStatus | None,
    ) -> list[dict[str, str]]:
        compensation_errors: list[dict[str, str]] = []
        self._collect_error(
            compensation_errors,
            "sandbox_state_rollback",
            lambda: self._repository.save_sandbox_state(
                agent_id,
                sandbox_payload_json=self._sandbox_handle_payload(sandbox_handle),
                adapter_base_url=adapter_base_url,
                adapter_ready=adapter_ready,
                last_error=last_error,
            ),
        )
        if status_on_error is not None:
            compensation_errors.extend(
                self._compensate_status_only(
                    agent_id=agent_id,
                    status=status_on_error,
                )
            )
        return compensation_errors

    def _compensate_status_only(
        self,
        *,
        agent_id: str,
        status: AgentStatus,
    ) -> list[dict[str, str]]:
        compensation_errors: list[dict[str, str]] = []
        self._collect_error(
            compensation_errors,
            "agent_status_error",
            lambda: self._repository.update_agent_status(agent_id, status),
        )
        return compensation_errors

    @staticmethod
    def _collect_error(
        errors: list[dict[str, str]],
        stage: str,
        action: Callable[[], Any],
    ) -> None:
        try:
            action()
        except Exception as exc:
            errors.append(AgentManager._cleanup_error(stage, exc))

    @staticmethod
    def _cleanup_error(stage: str, exc: Exception) -> dict[str, str]:
        """构造 cleanup 错误条目：DomainError 额外带上 code。

        code 是"哪些清理失败可以容忍"的判定依据（见 _is_tolerable_cleanup_error）；
        只留 message 文本会迫使上层去匹配错误文案。
        """
        entry = {"stage": stage, "error": AgentManager._error_message(exc)}
        if isinstance(exc, DomainError):
            entry["code"] = exc.code
        return entry

    @staticmethod
    def _error_message(exc: Exception) -> str:
        return exc.message if isinstance(exc, DomainError) else str(exc)

    @staticmethod
    def _runtime_skill_error_status_code(exc: httpx.HTTPError) -> int:
        """把 runtime 侧的失败映射为网关对外的状态码。

        技能安装/卸载失败此前统一落到 DomainError 的默认 400，把上游
        agent-server 已经分好类的语义（技能不存在 404 / source 非法 400 /
        hub 故障 502）全部抹平成"调用方请求有误"。这里按上游语义还原：

        * 上游 4xx 原样透传 —— 这些分类对调用方有意义（如 SKILL_NOT_FOUND 404）；
        * 上游 5xx 与连接层失败统一 502（坏上游，而非坏请求）；
        * 超时单独给 504，便于前端与告警区分"慢"和"错"。
        """
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if 400 <= status_code < 500:
                return status_code
            return 502
        if isinstance(exc, httpx.TimeoutException):
            return 504
        return 502

    @staticmethod
    def _build_runtime_skill_error_details(
        *,
        agent_id: str,
        skill_name: str,
        exc: httpx.HTTPError,
    ) -> dict[str, Any]:
        details = AgentManager._describe_http_error(agent_id=agent_id, exc=exc)
        details["skill_name"] = skill_name
        return details

    @staticmethod
    def _describe_http_error(
        *,
        agent_id: str,
        exc: httpx.HTTPError,
    ) -> dict[str, Any]:
        # str(HTTPStatusError) 只有 "Server error '500 ...'"，真正的根因（code /
        # message / details）全在响应体里。不解析响应体就等于把根因丢掉——服务重启时
        # "部分 agent 恢复失败" 曾因此完全不可诊断。
        details: dict[str, Any] = {
            "agent_id": agent_id,
            "error": str(exc),
        }
        if not isinstance(exc, httpx.HTTPStatusError):
            return details

        response = exc.response
        details["upstream_status_code"] = response.status_code

        payload = AgentManager._parse_http_error_payload(response)
        if payload is None:
            response_text = response.text.strip()
            if response_text:
                details["error"] = response_text
                details["upstream_response_text"] = response_text
            return details

        error_payload = AgentManager._extract_error_payload(payload)
        if error_payload is None:
            details["upstream_response"] = payload
            return details

        upstream_code = error_payload.get("code")
        if isinstance(upstream_code, str) and upstream_code:
            details["upstream_error_code"] = upstream_code

        upstream_request_id = error_payload.get("request_id")
        if isinstance(upstream_request_id, str) and upstream_request_id:
            details["upstream_request_id"] = upstream_request_id

        upstream_message = error_payload.get("message")
        if isinstance(upstream_message, str) and upstream_message:
            details["upstream_error_message"] = upstream_message

        upstream_details = error_payload.get("details")
        if isinstance(upstream_details, dict):
            details["upstream_error_details"] = upstream_details
            reason = upstream_details.get("reason")
            if isinstance(reason, str) and reason:
                details["error"] = reason
                return details

        if isinstance(upstream_message, str) and upstream_message:
            details["error"] = upstream_message

        return details

    @staticmethod
    def _parse_http_error_payload(response: httpx.Response) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _extract_error_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        nested_error = payload.get("error")
        if isinstance(nested_error, dict):
            return nested_error
        if isinstance(payload.get("code"), str) and isinstance(
            payload.get("message"), str
        ):
            return payload
        return None

    def _raise_operation_failed(
        self,
        *,
        code: str,
        message: str,
        agent_id: str,
        cause: Exception,
        cleanup_errors: list[dict[str, str]] | None = None,
        compensation_errors: list[dict[str, str]] | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "agent_id": agent_id,
            "cause": self._error_message(cause),
            "cleanup_errors": list(cleanup_errors or []),
        }
        if isinstance(cause, DomainError):
            details["cause_code"] = cause.code
        if compensation_errors:
            details["compensation_errors"] = list(compensation_errors)
        raise DomainError(code=code, message=message, details=details) from cause

    @staticmethod
    def _ensure_transition(agent: AgentRecord, target: AgentStatus) -> None:
        if can_transition(agent.status, target):
            return
        raise DomainError(
            code=INVALID_AGENT_TRANSITION,
            message="Agent status transition is not allowed.",
            details={
                "agent_id": agent.id,
                "from_status": agent.status.value,
                "to_status": target.value,
            },
        )

    @staticmethod
    def _sandbox_handle_payload(handle: SandboxHandle) -> dict[str, Any]:
        return {
            "sandbox_id": handle.sandbox_id,
            "agent_id": handle.agent_id,
            "workspace_path": handle.workspace_path,
            "metadata": dict(handle.metadata),
        }

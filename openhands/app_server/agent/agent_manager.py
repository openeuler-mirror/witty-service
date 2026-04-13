"""Agent Manager for Agent Middleware Service.

This module manages Agent lifecycle and coordinates with Sandbox and Adapter.

Design reference: Section 4.2 (class diagram) and Section 6 (development view).

The AgentManager:
- Creates/deletes Agents with associated Sandboxes and Sessions
- Manages Agent lifecycle (pause/resume)
- Sends messages to Agents via AdapterClient
- Manages session lifecycle via persistence store
"""

import asyncio
import logging
import uuid
from datetime import datetime
from functools import lru_cache
from typing import Any, AsyncIterator, Optional

from openhands.app_server.agent.adapter_client import get_adapter_client_pool
from openhands.app_server.agent.agent_entity import AgentEntity, SessionEntity
from openhands.app_server.agent.domain_models import Agent
from openhands.app_server.agent.models import (
    AgentInfo,
    AgentStatus,
    CreateAgentRequest,
    SessionInfo,
    UpdateAgentRequest,
)
from openhands.app_server.agent.state_machine import AgentEvent, transition
from openhands.app_server.agent.sqlite_store import AgentSqliteStore
from openhands.app_server.agent.workspace_storage import DefaultAgentWorkspaceStorage
from openhands.app_server.sandbox.agent_sandbox_service import (
    AgentSandboxFactory,
    SandboxInfo,
    SandboxStatus,
    WorkspaceMount,
)

logger = logging.getLogger(__name__)


class AgentManager:
    """Manages Agent lifecycle and coordinates with Sandbox and Adapter.

    This is a singleton class - only one instance exists per application.
    Use `AgentManager.get_instance()` to get the singleton instance.

    The AgentManager is responsible for:
    - Creating and deleting Agents
    - Managing Agent lifecycle (RUNNING, PAUSED, etc.)
    - Coordinating with SandboxBackend for sandbox lifecycle
    - Managing session lifecycle via persistence store
    - Sending messages to Agents via Adapter REST API
    """

    _instance: Optional["AgentManager"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        sandbox_factory=None,
        storage_service: Optional[Any] = None,
    ):
        if self._initialized:
            return
        self._initialized = True

        self._agents: dict[str, Agent] = {}
        self._sandbox_factory = sandbox_factory or AgentSandboxFactory
        self._storage_service = (
            storage_service if storage_service is not None else DefaultAgentWorkspaceStorage()
        )
        self._store = AgentSqliteStore()
        self._sandboxes: dict[str, SandboxInfo] = {}
        self._agent_backend_type: dict[str, str] = {}
        self._backends: dict[str, Any] = {}
        self._agent_configs: dict[str, dict[str, Any]] = {}
        self._adapter_pool = get_adapter_client_pool()
        self._creation_tasks: dict[str, asyncio.Task] = {}

        self._restore_from_store()

    def _store_payload_for_agent(self, agent_id: str) -> dict[str, Any]:
        agent = self._agents.get(agent_id)
        if not agent:
            raise KeyError(agent_id)
        payload = agent.model_dump()
        payload["status"] = agent.status.value
        payload["created_at"] = agent.created_at.isoformat()
        payload["updated_at"] = agent.updated_at.isoformat()
        payload["template"] = self._agent_configs.get(agent_id, {}).get("template")
        payload["model_override"] = self._agent_configs.get(agent_id, {}).get("model_override")
        payload["sandbox_config"] = self._agent_configs.get(agent_id, {}).get("sandbox_config")
        return payload

    async def _append_creation_log(self, agent_id: str, message: str) -> None:
        agent = self._agents.get(agent_id)
        if not agent:
            return
        ts = datetime.now().isoformat(timespec="seconds")
        line = f"[{ts}] {message}"
        agent.creation_log = (agent.creation_log + [line])[-200:]
        agent.updated_at = datetime.now()
        logger.info("agent %s provision: %s", agent_id, message)

    async def _set_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        agent = self._agents.get(agent_id)
        if not agent:
            return
        agent.status = status
        agent.updated_at = datetime.now()
        await self._storage_service.save_state(agent_id, agent.model_dump())
        self._store.upsert_agent(self._store_payload_for_agent(agent_id))

    async def _apply_event(self, agent_id: str, event: AgentEvent) -> None:
        agent = self._agents.get(agent_id)
        if not agent:
            return
        next_status = transition(agent.status, event)
        await self._set_agent_status(agent_id, next_status)

    async def _wait_until_agent_running(
        self, agent_id: str, timeout_seconds: float = 10.0, poll_interval: float = 0.2
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            agent = self._agents.get(agent_id)
            if not agent:
                return False
            if agent.status == AgentStatus.RUNNING:
                sandbox_info = self._sandboxes.get(agent_id)
                if not sandbox_info or not sandbox_info.adapter_url:
                    return True
                try:
                    adapter_client = self._adapter_pool.get_client(
                        sandbox_info.adapter_url, agent_id
                    )
                    status_resp = await adapter_client.get_status()
                    if isinstance(status_resp, dict) and str(
                        status_resp.get("status", "")
                    ).upper() in {"RUNNING", "READY"}:
                        return True
                except Exception:
                    # Adapter status endpoint may be temporarily unavailable during startup.
                    pass
            await asyncio.sleep(poll_interval)
        return False

    def _restore_from_store(self) -> None:
        for row in self._store.list_agents():
            raw_status = row["status"]
            if raw_status == "CREATING":
                status = AgentStatus.ERROR
                creation_error = "Service restarted during agent provisioning"
            else:
                status = AgentStatus.PAUSED if raw_status in {"RUNNING", "PAUSED"} else AgentStatus(raw_status)
                creation_error = None
            agent = Agent(
                id=row["id"],
                name=row["name"],
                adapter_type=row["adapter_type"],
                status=status,
                sandbox_id="",
                default_session_id=row["default_session_id"] or "",
                has_scheduled_tasks=row["has_scheduled_tasks"],
                idle_timeout=row["idle_timeout"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                creation_error=creation_error,
            )
            self._agents[agent.id] = agent
            self._agent_backend_type[agent.id] = (row.get("sandbox_config") or {}).get("type", "docker")
            self._agent_configs[agent.id] = {
                "template": row.get("template"),
                "model_override": row.get("model_override"),
                "sandbox_config": row.get("sandbox_config"),
            }
            if raw_status == "CREATING":
                self._store.upsert_agent(self._store_payload_for_agent(agent.id))

    def _get_backend(self, sandbox_type: str):
        if sandbox_type not in self._backends:
            self._backends[sandbox_type] = self._sandbox_factory.create(sandbox_type=sandbox_type)
        return self._backends[sandbox_type]

    def _host_workspace_path(self, agent_id: str) -> str:
        getter = getattr(self._storage_service, "host_workspace_path", None)
        if callable(getter):
            return getter(agent_id)
        return f"/tmp/agent-workspaces/{agent_id}"

    def _get_agent_entity(self, agent_id: str) -> AgentEntity | None:
        info = self._agents.get(agent_id)
        if info is None:
            return None
        return AgentEntity(
            info=info,
            store=self._store,
            adapter_pool=self._adapter_pool,
            get_sandbox_info=lambda aid: self._sandboxes.get(aid),
            wait_until_running=self._wait_until_agent_running,
        )

    async def _get_session_entity(self, agent_id: str, session_id: str) -> SessionEntity | None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        agent_entity = self._get_agent_entity(agent_id)
        if agent_entity is None:
            return None
        session = await agent_entity.get_session(session_id)
        if session is None:
            return None
        return SessionEntity(
            agent=agent,
            session=session,
            store=self._store,
            adapter_pool=self._adapter_pool,
            get_sandbox_info=lambda aid: self._sandboxes.get(aid),
            wait_until_running=self._wait_until_agent_running,
            resume_agent=self.resume_agent,
        )

    @classmethod
    @lru_cache(maxsize=1)
    def get_instance(cls) -> "AgentManager":
        """Get the singleton instance of AgentManager."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance. For testing purposes."""
        inst = cls._instance
        if inst is not None:
            tasks = getattr(inst, "_creation_tasks", {}) or {}
            for t in list(tasks.values()):
                if not t.done():
                    t.cancel()
        cls._instance = None
        # Clear the cache so that get_instance will create a new instance
        cls.get_instance.cache_clear()

    async def create_agent(self, request: CreateAgentRequest) -> AgentInfo:
        """Register an agent and return immediately with status CREATING.

        Provisioning (workspace, sandbox, adapter, default session) continues in a
        background task. Poll ``GET /agents/{id}`` for ``status``, ``creation_log``,
        and ``creation_error``.
        """
        agent_id = str(uuid.uuid4())
        sandbox_type = request.sandbox_config.type if request.sandbox_config else "docker"

        agent = Agent(
            id=agent_id,
            name=request.name,
            adapter_type=request.adapter_type.value,
            status=AgentStatus.CREATING,
            sandbox_id="",
            default_session_id="",
            has_scheduled_tasks=False,
            idle_timeout=request.idle_timeout,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        self._agents[agent_id] = agent
        self._agent_configs[agent_id] = {
            "template": request.template,
            "model_override": request.model_override.model_dump() if request.model_override else None,
            "sandbox_config": request.sandbox_config.model_dump() if request.sandbox_config else {"type": sandbox_type},
        }
        self._agent_backend_type[agent_id] = sandbox_type

        await self._append_creation_log(agent_id, "Agent registered; provisioning queued")
        await self._storage_service.save_state(agent_id, agent.model_dump())
        self._store.upsert_agent(self._store_payload_for_agent(agent_id))

        task = asyncio.create_task(self._complete_agent_creation(agent_id, request))
        self._creation_tasks[agent_id] = task

        def _provision_done(t: asyncio.Task) -> None:
            self._creation_tasks.pop(agent_id, None)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error("Agent %s provisioning task failed unexpectedly: %s", agent_id, exc)

        task.add_done_callback(_provision_done)
        return agent.to_info()

    async def _complete_agent_creation(self, agent_id: str, request: CreateAgentRequest) -> None:
        agent = self._agents.get(agent_id)
        if not agent:
            return

        sandbox_type = request.sandbox_config.type if request.sandbox_config else "docker"
        workspace_path = self._host_workspace_path(agent_id)

        try:
            await self._append_creation_log(agent_id, "Initializing workspace directory")
            await self._storage_service.init_workspace(agent_id, workspace_path)

            await self._append_creation_log(agent_id, "Starting sandbox")
            sandbox = self._get_backend(sandbox_type)
            workspace_mount = WorkspaceMount(
                host_path=workspace_path,
                guest_path="/workspace",
            )
            adapter_config = {
                "agent_id": agent_id,
                "agent_type": request.adapter_type.value,
                "config": {
                    "template": request.template,
                    "model_override": request.model_override.model_dump() if request.model_override else None,
                },
            }

            sandbox_info = await sandbox.start(
                sandbox_type=sandbox_type,
                workspace_mount=workspace_mount,
                adapter_config=adapter_config,
                options={},
            )

            agent.sandbox_id = sandbox_info.id
            self._sandboxes[agent_id] = sandbox_info

            if not sandbox_info.adapter_url:
                err = f"adapter_url missing for sandbox {sandbox_info.id}"
                logger.error("No adapter_url for sandbox %s (agent %s)", sandbox_info.id, agent_id)
                await self._append_creation_log(agent_id, err)
                agent.creation_error = err
                agent.status = transition(agent.status, AgentEvent.CREATE_FAILED)
                agent.updated_at = datetime.now()
                if agent_id in self._sandboxes:
                    del self._sandboxes[agent_id]
                if agent_id in self._agent_backend_type:
                    del self._agent_backend_type[agent_id]
                if agent_id in self._agent_configs:
                    del self._agent_configs[agent_id]
                await self._storage_service.save_state(agent_id, agent.model_dump())
                self._store.upsert_agent(self._store_payload_for_agent(agent_id))
                return

            await self._append_creation_log(
                agent_id, "Sandbox running; starting adapter inside sandbox"
            )
            adapter_client = self._adapter_pool.get_client(sandbox_info.adapter_url, agent_id)
            await adapter_client.start_agent(
                agent_id=agent_id,
                agent_type=request.adapter_type.value,
                config=adapter_config["config"],
                workspace_path=workspace_mount.guest_path,
                restore=False,
            )

            await self._append_creation_log(
                agent_id, "Adapter process started; transitioning to RUNNING"
            )
            await self._apply_event(agent_id, AgentEvent.CREATE_SUCCEEDED)

            await self._append_creation_log(agent_id, "Creating default session")
            session = await self.create_session(agent_id)
            agent.default_session_id = session.id
            self._agents[agent_id] = agent
            await self._storage_service.save_state(agent_id, agent.model_dump())
            self._store.upsert_agent(self._store_payload_for_agent(agent_id))

            await self._append_creation_log(
                agent_id, f"Agent ready (sandbox_id={agent.sandbox_id})"
            )
            logger.info("Created agent %s with sandbox %s", agent_id, agent.sandbox_id)

        except asyncio.CancelledError:
            await self._append_creation_log(agent_id, "Provisioning cancelled")
            raise
        except Exception as e:
            logger.error("Failed to provision agent %s: %s", agent_id, e)
            await self._append_creation_log(agent_id, f"Failed: {e}")
            ag = self._agents.get(agent_id)
            if ag:
                ag.creation_error = str(e)
                if ag.status == AgentStatus.CREATING:
                    ag.status = transition(ag.status, AgentEvent.CREATE_FAILED)
                else:
                    ag.status = AgentStatus.ERROR
                ag.updated_at = datetime.now()
                await self._storage_service.save_state(agent_id, ag.model_dump())
                self._store.upsert_agent(self._store_payload_for_agent(agent_id))

    async def get_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Get agent by ID.

        Args:
            agent_id: The agent ID.

        Returns:
            AgentInfo if found, None otherwise.
        """
        agent = self._agents.get(agent_id)
        return agent.to_info() if agent else None

    async def list_agents(self) -> list[AgentInfo]:
        """List all agents.

        Returns:
            List of AgentInfo objects.
        """
        logger.info(f"Listing agents, found {len(self._agents)} agents in memory")
        try:
            agents = []
            for agent in self._agents.values():
                logger.debug(f"Processing agent: {agent.id}, status: {agent.status}")
                agent_info = agent.to_info()
                agents.append(agent_info)
            logger.info(f"Successfully converted {len(agents)} agents to AgentInfo")
            return agents
        except Exception as e:
            logger.error(f"Error listing agents: {e}", exc_info=True)
            raise

    async def update_agent(self, agent_id: str, request: UpdateAgentRequest) -> Optional[AgentInfo]:
        """Update agent configuration.

        Args:
            agent_id: The agent ID.
            request: UpdateAgentRequest with updated fields.

        Returns:
            Updated AgentInfo if found, None otherwise.
        """
        agent = self._agents.get(agent_id)
        if not agent:
            return None

        if request.name is not None:
            agent.name = request.name
        if request.model_override is not None:
            sandbox_info = self._sandboxes.get(agent_id)
            if sandbox_info and sandbox_info.adapter_url:
                adapter_client = self._adapter_pool.get_client(sandbox_info.adapter_url, agent_id)
                await adapter_client.update_config({"model_override": request.model_override.model_dump()})
            self._agent_configs.setdefault(agent_id, {})["model_override"] = request.model_override.model_dump()
        if request.idle_timeout is not None:
            agent.idle_timeout = request.idle_timeout

        agent.updated_at = datetime.now()

        await self._storage_service.save_state(agent_id, agent.model_dump())
        self._store.upsert_agent(self._store_payload_for_agent(agent_id))

        return agent.to_info()

    async def delete_agent(self, agent_id: str) -> None:
        """Delete an agent.

        This method:
        1. Stops the sandbox
        2. Cleans up the workspace
        3. Removes agent from storage

        Args:
            agent_id: The agent ID.
        """
        agent = self._agents.get(agent_id)
        if not agent:
            return

        task = self._creation_tasks.pop(agent_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if agent.sandbox_id and agent_id in self._sandboxes:
            try:
                sandbox_type = self._agent_backend_type.get(agent_id, "docker")
                sandbox = self._get_backend(sandbox_type)
                await sandbox.stop(agent.sandbox_id)
            except Exception as e:
                logger.error(f"Failed to stop sandbox {agent.sandbox_id}: {e}")
                raise
            del self._sandboxes[agent_id]

        await self._storage_service.cleanup(agent_id)

        await self._apply_event(agent_id, AgentEvent.DELETE_REQUESTED)
        self._agents.pop(agent_id, None)
        self._agent_backend_type.pop(agent_id, None)
        self._agent_configs.pop(agent_id, None)
        self._store.delete_agent(agent_id)

        logger.info(f"Deleted agent {agent_id}")

    async def pause_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Pause an agent.

        Pausing an agent:
        1. Stops the adapter (which saves state to workspace)
        2. Pauses the sandbox
        3. Updates agent status to PAUSED

        Args:
            agent_id: The agent ID.

        Returns:
            Updated AgentInfo if found, None otherwise.
        """
        agent = self._agents.get(agent_id)
        if not agent:
            return None

        if agent.status != AgentStatus.RUNNING:
            return None

        sandbox_info = self._sandboxes.get(agent_id)
        if sandbox_info and sandbox_info.adapter_url:
            try:
                adapter_client = self._adapter_pool.get_client(
                    sandbox_info.adapter_url, agent_id
                )
                await adapter_client.stop_agent()
            except Exception as e:
                logger.error(f"Failed to stop adapter for agent {agent_id}: {e}")
                raise

        if agent_id in self._sandboxes:
            try:
                sandbox_type = self._agent_backend_type.get(agent_id, "docker")
                sandbox = self._get_backend(sandbox_type)
                if hasattr(sandbox, "pause"):
                    await sandbox.pause(agent.sandbox_id)
                else:
                    await sandbox.stop(agent.sandbox_id)
            except Exception as e:
                logger.error(f"Failed to pause sandbox {agent.sandbox_id}: {e}")
                raise

        await self._apply_event(agent_id, AgentEvent.PAUSE_REQUESTED)

        logger.info(f"Paused agent {agent_id}")
        return agent.to_info()

    async def resume_agent(self, agent_id: str) -> Optional[AgentInfo]:
        """Resume a paused agent.

        Resuming an agent:
        1. Resumes the sandbox
        2. Restarts the adapter with restore=true
        3. Updates agent status to RUNNING

        Args:
            agent_id: The agent ID.

        Returns:
            Updated AgentInfo if found, None otherwise.
        """
        agent = self._agents.get(agent_id)
        if not agent:
            return None

        if agent.status != AgentStatus.PAUSED:
            return None

        sandbox_type = self._agent_backend_type.get(agent_id, "docker")
        if agent_id not in self._sandboxes:
            backend = self._get_backend(sandbox_type)
            workspace_path = self._host_workspace_path(agent_id)
            workspace_mount = WorkspaceMount(host_path=workspace_path, guest_path="/workspace")
            adapter_config = {
                "agent_id": agent_id,
                "agent_type": agent.adapter_type,
                "config": {
                    "template": self._agent_configs.get(agent_id, {}).get("template"),
                    "model_override": self._agent_configs.get(agent_id, {}).get("model_override"),
                },
            }
            sandbox_info = await backend.start(
                sandbox_type=sandbox_type,
                workspace_mount=workspace_mount,
                adapter_config=adapter_config,
                options={},
            )
            self._sandboxes[agent_id] = sandbox_info
            agent.sandbox_id = sandbox_info.id

        if agent_id in self._sandboxes:
            try:
                sandbox = self._get_backend(sandbox_type)
                await sandbox.resume(agent.sandbox_id)
            except Exception as e:
                logger.error(f"Failed to resume sandbox {agent.sandbox_id}: {e}")
                raise

        sandbox_info = self._sandboxes.get(agent_id)
        if sandbox_info and sandbox_info.adapter_url:
            try:
                adapter_client = self._adapter_pool.get_client(
                    sandbox_info.adapter_url, agent_id
                )
                await adapter_client.start_agent(
                    agent_id=agent_id,
                    agent_type=agent.adapter_type,
                    config={
                        "template": self._agent_configs.get(agent_id, {}).get("template"),
                        "model_override": self._agent_configs.get(agent_id, {}).get("model_override"),
                    },
                    workspace_path=(
                        sandbox_info.workspace_mount.guest_path
                        if sandbox_info and hasattr(sandbox_info, "workspace_mount")
                        else "/workspace"
                    ),
                    restore=True,
                )
            except Exception as e:
                logger.error(f"Failed to restart adapter for agent {agent_id}: {e}")
                raise

        await self._apply_event(agent_id, AgentEvent.RESUME_REQUESTED)

        logger.info(f"Resumed agent {agent_id}")
        return agent.to_info()

    async def send_message(
        self, agent_id: str, session_id: str, content: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a message to an agent.

        This method:
        1. Validates agent and session exist
        2. Auto-resumes agent if paused
        3. Forwards message to adapter via AdapterClient
        4. Yields streaming events

        Args:
            agent_id: The agent ID.
            session_id: The session ID.
            content: The message content.

        Yields:
            Agent events as dictionaries (type, content, timestamp, etc.).
        """
        agent = self._agents.get(agent_id)
        if not agent:
            yield {"type": "error", "content": f"Agent not found: {agent_id}", "timestamp": datetime.now().isoformat()}
            return

        session_entity = await self._get_session_entity(agent_id, session_id)
        if not session_entity:
            yield {"type": "error", "content": f"Session not found: {session_id}", "timestamp": datetime.now().isoformat()}
            return

        async for event in session_entity.send_message(content):
            yield event
        return

    async def send_message_ws(
        self, agent_id: str, session_id: str, content: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a message to adapter over WebSocket passthrough."""
        agent = self._agents.get(agent_id)
        if not agent:
            yield {"type": "error", "content": f"Agent not found: {agent_id}", "timestamp": datetime.now().isoformat()}
            return

        session = await self.get_session(agent_id, session_id)
        if not session:
            yield {"type": "error", "content": f"Session not found: {session_id}", "timestamp": datetime.now().isoformat()}
            return

        if agent.status == AgentStatus.PAUSED:
            await self.resume_agent(agent_id)

        running = await self._wait_until_agent_running(agent_id)
        if not running:
            yield {"type": "error", "content": f"Agent not running (status: {agent.status})", "timestamp": datetime.now().isoformat()}
            return

        sandbox_info = self._sandboxes.get(agent_id)
        if not sandbox_info or not sandbox_info.adapter_url:
            yield {
                "type": "error",
                "content": f"Adapter endpoint unavailable for agent {agent_id}",
                "timestamp": datetime.now().isoformat(),
            }
            return

        try:
            self._store.add_message(
                message_id=str(uuid.uuid4()),
                session_id=session_id,
                role="user",
                content=content,
            )
            adapter_client = self._adapter_pool.get_client(
                sandbox_info.adapter_url, agent_id
            )
            async for event in adapter_client.send_message_ws(content, session_id):
                self._store.add_message(
                    message_id=str(uuid.uuid4()),
                    session_id=session_id,
                    role="assistant",
                    content=event.get("content", ""),
                    event_type=event.get("type"),
                    payload=event,
                )
                yield event
            return
        except Exception as e:
            logger.error(f"Failed to send message via adapter WS: {e}")
            yield {"type": "error", "content": str(e), "timestamp": datetime.now().isoformat()}
            return

    async def create_session(self, agent_id: str, session_id: str = None) -> SessionInfo:
        """Create a new session for an agent.

        Args:
            agent_id: The agent ID.
            session_id: Optional specific session ID.

        Returns:
            SessionInfo for the created session.
        """
        agent = self._get_agent_entity(agent_id)
        if agent is None:
            raise ValueError(f"Agent not found: {agent_id}")
        session = await agent.create_session(session_id)
        return session.to_info()

    async def get_session(self, agent_id: str, session_id: str) -> Optional[SessionInfo]:
        """Get session by ID.

        Args:
            agent_id: The agent ID.
            session_id: The session ID.

        Returns:
            SessionInfo if found, None otherwise.
        """
        agent = self._get_agent_entity(agent_id)
        if agent is None:
            return None
        session = await agent.get_session(session_id)
        return session.to_info() if session else None

    async def list_sessions(self, agent_id: str) -> list[SessionInfo]:
        """List all sessions for an agent.

        Args:
            agent_id: The agent ID.

        Returns:
            List of SessionInfo objects.
        """
        agent = self._get_agent_entity(agent_id)
        if agent is None:
            return []
        sessions = await agent.list_sessions()
        return [s.to_info() for s in sessions]

    async def delete_session(self, agent_id: str, session_id: str) -> None:
        """Delete a session.

        Args:
            agent_id: The agent ID.
            session_id: The session ID.

        Raises:
            ValueError: If agent not found.
        """
        agent = self._get_agent_entity(agent_id)
        if agent is None:
            raise ValueError(f"Agent not found: {agent_id}")
        await agent.delete_session(session_id)

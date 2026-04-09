"""Agent domain entity behaviors."""

import logging
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from openhands.app_server.agent.domain_models import Agent, Message, Session
from openhands.app_server.agent.models import AgentStatus, SessionStatus
from openhands.app_server.agent.sqlite_store import AgentSqliteStore

logger = logging.getLogger(__name__)


class AgentEntity:
    """Domain behaviors that belong to an Agent aggregate."""

    def __init__(
        self,
        info: Agent,
        store: AgentSqliteStore,
        adapter_pool: Any,
        get_sandbox_info: Callable[[str], Any],
        wait_until_running: Callable[[str], Awaitable[bool]],
    ) -> None:
        self.info = info
        self._store = store
        self._adapter_pool = adapter_pool
        self._get_sandbox_info = get_sandbox_info
        self._wait_until_running = wait_until_running

    async def create_session(self, session_id: str | None = None) -> Session:
        if session_id is None:
            session_id = str(uuid.uuid4())

        sandbox_info = self._get_sandbox_info(self.info.id)
        if not sandbox_info or not sandbox_info.adapter_url:
            raise RuntimeError(f"Adapter unavailable for agent {self.info.id}")

        running = await self._wait_until_running(self.info.id)
        if not running:
            raise RuntimeError(f"Agent {self.info.id} is not ready for session creation")

        adapter_client = self._adapter_pool.get_client(sandbox_info.adapter_url, self.info.id)
        await adapter_client.create_session(session_id)

        session = Session(
            id=session_id,
            agent_id=self.info.id,
            status=SessionStatus.ACTIVE,
            created_at=datetime.now(),
        )
        self._store.upsert_session_obj(session)
        return session

    async def get_session(self, session_id: str) -> Session | None:
        row = self._store.get_session(self.info.id, session_id)
        if row is None:
            return None
        return Session(
            id=row["id"],
            agent_id=row["agent_id"],
            status=SessionStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def list_sessions(self) -> list[Session]:
        rows = self._store.list_sessions(self.info.id)
        return [
            Session(
                id=row["id"],
                agent_id=row["agent_id"],
                status=SessionStatus(row["status"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    async def delete_session(self, session_id: str) -> None:
        sandbox_info = self._get_sandbox_info(self.info.id)
        if sandbox_info and sandbox_info.adapter_url:
            try:
                adapter_client = self._adapter_pool.get_client(sandbox_info.adapter_url, self.info.id)
                await adapter_client.delete_session(session_id)
            except Exception as e:
                logger.warning(f"Failed to delete session via adapter API: {e}")

        self._store.delete_session(session_id)


class SessionEntity:
    """Domain behaviors that belong to a Session entity."""

    def __init__(
        self,
        agent: Agent,
        session: Session,
        store: AgentSqliteStore,
        adapter_pool: Any,
        get_sandbox_info: Callable[[str], Any],
        wait_until_running: Callable[[str], Awaitable[bool]],
        resume_agent: Callable[[str], Awaitable[Any]],
    ) -> None:
        self.agent = agent
        self.session = session
        self._store = store
        self._adapter_pool = adapter_pool
        self._get_sandbox_info = get_sandbox_info
        self._wait_until_running = wait_until_running
        self._resume_agent = resume_agent

    async def send_message(self, content: str) -> Any:
        if self.agent.status == AgentStatus.PAUSED:
            await self._resume_agent(self.agent.id)

        running = await self._wait_until_running(self.agent.id)
        if not running:
            yield {
                "type": "error",
                "content": f"Agent not running (status: {self.agent.status})",
                "timestamp": datetime.now().isoformat(),
            }
            return

        sandbox_info = self._get_sandbox_info(self.agent.id)
        if not sandbox_info or not sandbox_info.adapter_url:
            yield {
                "type": "error",
                "content": f"Adapter endpoint unavailable for agent {self.agent.id}",
                "timestamp": datetime.now().isoformat(),
            }
            return

        try:
            self._store.add_message_obj(
                Message(
                    id=str(uuid.uuid4()),
                    session_id=self.session.id,
                    role="user",
                    content=content,
                )
            )
            adapter_client = self._adapter_pool.get_client(sandbox_info.adapter_url, self.agent.id)
            async for event in adapter_client.send_message_stream(content, self.session.id):
                self._store.add_message_obj(
                    Message(
                        id=str(uuid.uuid4()),
                        session_id=self.session.id,
                        role="assistant",
                        content=event.get("content", ""),
                        event_type=event.get("type"),
                        payload=event,
                    )
                )
                yield event
        except Exception as e:
            logger.error(f"Failed to send message via adapter API: {e}")
            yield {"type": "error", "content": str(e), "timestamp": datetime.now().isoformat()}

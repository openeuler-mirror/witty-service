"""ORM persistence store for agent runtime data."""

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openhands.app_server.agent.domain_models import Agent, Message, Session
from openhands.app_server.agent.store.database import create_agentd_engine, create_agentd_session_factory
from openhands.app_server.agent.store.models import AgentdAgent, AgentdSession, AgentdSessionMessage
from openhands.app_server.utils.sql_utils import Base


class AgentSqliteStore:
    """Persistence layer for agent/session/message data."""

    def __init__(self, db_path: str | None = None):
        if db_path is None and os.getenv("PYTEST_CURRENT_TEST"):
            default_path = f"/tmp/agent-workspaces/agentd-test-{uuid.uuid4().hex}.sqlite3"
        else:
            default_path = "/tmp/agent-workspaces/agentd.sqlite3"
        self._db_path = Path(db_path or os.getenv("AGENTD_SQLITE_PATH", default_path))
        # Ensure the directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_agentd_engine(str(self._db_path))
        Base.metadata.create_all(self._engine)
        self._session_factory = create_agentd_session_factory(str(self._db_path))

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    def upsert_agent(self, agent: dict[str, Any]) -> None:
        with self._session_factory() as session:
            obj = session.get(AgentdAgent, agent["id"])
            created_at = agent.get("created_at")
            updated_at = agent.get("updated_at")
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            if isinstance(updated_at, str):
                updated_at = datetime.fromisoformat(updated_at)
            if obj is None:
                obj = AgentdAgent(id=agent["id"])
                session.add(obj)
            obj.name = agent["name"]
            obj.adapter_type = agent["adapter_type"]
            obj.status = agent["status"]
            obj.sandbox_id = agent.get("sandbox_id", "")
            obj.default_session_id = agent.get("default_session_id", "")
            obj.template = agent.get("template")
            obj.model_override = agent.get("model_override")
            obj.sandbox_config = agent.get("sandbox_config")
            obj.idle_timeout = int(agent.get("idle_timeout", 300))
            obj.has_scheduled_tasks = bool(agent.get("has_scheduled_tasks"))
            obj.created_at = created_at or self._utcnow()
            obj.updated_at = updated_at or self._utcnow()
            # default_session_id is kept in manager payload and consumed there.
            session.commit()

    def upsert_agent_obj(self, agent: Agent) -> None:
        self.upsert_agent(
            {
                **agent.model_dump(),
                "status": agent.status.value,
                "created_at": agent.created_at.isoformat(),
                "updated_at": agent.updated_at.isoformat(),
            }
        )

    def list_agents(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.query(AgentdAgent).all()
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "adapter_type": row.adapter_type,
                    "status": row.status,
                    "sandbox_id": row.sandbox_id or "",
                    "default_session_id": row.default_session_id or "",
                    "has_scheduled_tasks": bool(row.has_scheduled_tasks),
                    "idle_timeout": row.idle_timeout,
                    "template": row.template,
                    "model_override": row.model_override,
                    "sandbox_config": row.sandbox_config,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]

    def delete_agent(self, agent_id: str) -> None:
        with self._session_factory() as session:
            obj = session.get(AgentdAgent, agent_id)
            if obj is not None:
                session.delete(obj)
                session.commit()

    def upsert_session(self, session: dict[str, Any]) -> None:
        with self._session_factory() as db:
            obj = db.get(AgentdSession, session["id"])
            created_at = session.get("created_at")
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            if obj is None:
                obj = AgentdSession(
                    id=session["id"],
                    agent_id=session["agent_id"],
                    created_at=created_at or self._utcnow(),
                )
                db.add(obj)
            obj.status = session["status"]
            db.commit()

    def upsert_session_obj(self, session: Session) -> None:
        self.upsert_session(
            {
                **session.model_dump(),
                "status": session.status.value,
                "created_at": session.created_at.isoformat(),
            }
        )

    def list_sessions(self, agent_id: str) -> list[dict[str, Any]]:
        with self._session_factory() as db:
            rows = (
                db.query(AgentdSession)
                .filter(AgentdSession.agent_id == agent_id)
                .order_by(AgentdSession.created_at)
                .all()
            )
            return [
                {
                    "id": row.id,
                    "agent_id": row.agent_id,
                    "status": row.status,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def get_session(self, agent_id: str, session_id: str) -> dict[str, Any] | None:
        with self._session_factory() as db:
            row = (
                db.query(AgentdSession)
                .filter(
                    AgentdSession.agent_id == agent_id,
                    AgentdSession.id == session_id,
                )
                .one_or_none()
            )
            if row is None:
                return None
            return {
                "id": row.id,
                "agent_id": row.agent_id,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
            }

    def delete_session(self, session_id: str) -> None:
        with self._session_factory() as db:
            obj = db.get(AgentdSession, session_id)
            if obj is not None:
                db.delete(obj)
                db.commit()

    def add_message(
        self,
        message_id: str,
        session_id: str,
        role: str,
        content: str,
        event_type: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._session_factory() as db:
            obj = AgentdSessionMessage(
                id=message_id,
                session_id=session_id,
                role=role,
                content=content,
                content_type=event_type or "text",
                extra_data=payload or {},
                created_at=self._utcnow(),
            )
            db.add(obj)
            db.commit()

    def add_message_obj(self, message: Message) -> None:
        self.add_message(
            message_id=message.id,
            session_id=message.session_id,
            role=message.role,
            content=message.content,
            event_type=message.event_type,
            payload=message.payload,
        )

    def list_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._session_factory() as db:
            rows = (
                db.query(AgentdSessionMessage)
                .filter(AgentdSessionMessage.session_id == session_id)
                .order_by(AgentdSessionMessage.created_at.desc())
                .limit(limit)
                .all()
            )
            rows = list(reversed(rows))
            return [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "role": row.role,
                    "content": row.content,
                    "event_type": row.content_type,
                    "payload_json": row.extra_data,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

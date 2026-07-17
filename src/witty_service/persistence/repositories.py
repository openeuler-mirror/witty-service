from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from witty_service.domain.errors import session_not_found
from witty_service.domain.enums import AgentStatus
from witty_service.persistence.orm import (
    AgentORM,
    AgentSkillORM,
    AgentRuntimeStateORM,
    McpServerORM,
    MessageEventORM,
    MessageORM,
    MessageStatus,
    ModelORM,
    SessionORM,
    SessionStatus,
    SkillORM,
    SkillRepositoryORM,
)
from witty_service.sandbox.base import SandboxHandle


@dataclass(slots=True)
class AgentRecord:
    id: str
    name: str
    description: str
    sandbox_type: str
    adapter_type: str
    status: AgentStatus
    sandbox_id: str | None
    workspace_path: str
    idle_timeout_seconds: int
    has_scheduled_tasks: bool
    model_id: str | None
    mcp_server_list: list[str]
    last_active_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class ModelRecord:
    id: str
    name: str
    provider: str
    api_key: str
    api_base_url: str | None
    compatibility: str | None
    enabled: bool
    max_tokens: int
    temperature: float
    is_default: bool
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class McpServerRecord:
    id: str
    mcp_server_name: str
    mcp_server_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SessionRecord:
    id: str
    agent_id: str
    remote_runtime_agent_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    runtime_type: str | None = None
    runtime_session_id: str | None = None
    runtime_session_key: str | None = None
    title: str | None = None
    pinned: bool = False


@dataclass(slots=True)
class SandboxStateRecord:
    agent_id: str
    sandbox_payload_json: dict[str, Any]
    adapter_base_url: str | None
    adapter_ready: bool
    last_error: str | None

    @property
    def handle(self) -> SandboxHandle:
        payload = dict(self.sandbox_payload_json)
        return SandboxHandle(
            sandbox_id=str(payload["sandbox_id"]),
            agent_id=str(payload.get("agent_id", self.agent_id)),
            workspace_path=str(payload["workspace_path"]),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class AgentWithRuntimeStateRecord:
    agent: AgentRecord
    runtime_state: SandboxStateRecord | None


@dataclass(slots=True)
class SkillRepositoryRecord:
    repo_id: str
    repo_name: str
    source_type: str
    branch: str | None
    url: str | None
    local_path: str | None
    skill_discover_status: str
    skill_num: int
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SkillRecord:
    skill_id: str
    repo_id: str | None
    skill_name: str
    relative_path: str | None
    metadata: dict[str, Any]
    skill_source: str | None
    skill_md_url: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class AgentSkillRecord:
    agent_id: str
    skill_id: str
    source_type: str
    repo_id: str | None
    skill_name: str
    installed_at: datetime
    relative_path: str | None = None
    metadata: dict[str, Any] | None = None
    skill_source: str | None = None
    skill_md_url: str | None = None


def _format_utc_datetime(dt: datetime) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _assemble_message(msg: MessageORM, events: list[MessageEventORM]) -> dict[str, Any]:
    tool_calls: list[dict[str, Any]] = []
    tool_calls_by_id: dict[str, dict[str, Any]] = {}
    thinking: list[str] = []
    usage: dict[str, Any] | None = None
    event_items: list[dict[str, Any]] = []

    for evt in events:
        payload = dict(evt.payload_json or {})
        item: dict[str, Any] = {
            "type": evt.event_type,
            "timestamp": _format_utc_datetime(evt.created_at),
        }

        if evt.event_type == "tool.call.started":
            tool_name = payload.get("tool_name", "")
            tool_call_id = payload.get("tool_call_id", "")
            tc: dict[str, Any] = {
                "id": tool_call_id,
                "name": tool_name,
                "status": "running",
                "input": payload.get("arguments"),
            }
            tool_calls.append(tc)
            tool_calls_by_id[tool_call_id] = tc
            item["toolCall"] = {
                "id": tool_call_id,
                "name": tool_name,
                "status": "running",
                "input": payload.get("arguments"),
            }

        elif evt.event_type == "tool.call.response":
            tool_call_id = payload.get("tool_call_id", "")
            if tool_call_id and tool_call_id in tool_calls_by_id:
                tc = tool_calls_by_id[tool_call_id]
                tc["status"] = "completed" if not payload.get("is_error") else "error"
                tc["output"] = payload.get("content")
                tc["duration"] = payload.get("duration")
                if payload.get("is_error"):
                    tc["error"] = payload.get("content")
                item["toolCall"] = {
                    "id": tool_call_id,
                    "name": tc.get("name", ""),
                    "status": tc["status"],
                    "input": tc.get("input"),
                    "output": payload.get("content"),
                    "error": payload.get("content") if payload.get("is_error") else None,
                    "duration": payload.get("duration"),
                }

        elif evt.event_type == "thinking":
            content = payload.get("thinking", "") 
            if content:
                thinking.append(content)
            item["content"] = content
        
        elif evt.event_type in ("message.delta", "thinking.delta"):
            continue

        elif evt.event_type == "usage.updated":
            usage = {
                "inputTokens": payload.get("input_tokens"),
                "outputTokens": payload.get("output_tokens"),
                "totalCost": payload.get("total_cost"),
            }
            item["usage"] = usage
        event_items.append(item)

    msg_status = msg.status.value if isinstance(msg.status, MessageStatus) else msg.status
    if msg_status in ("interrupted", "error"):
        for tc in tool_calls:
            if tc.get("status") == "running":
                tc["status"] = "error"
                if "error" not in tc:
                    tc["error"] = "已停止生成" if msg_status == "interrupted" else "发生错误"
        for item in event_items:
            tc = item.get("toolCall")
            if tc and tc.get("status") == "running":
                tc["status"] = "error"
                if "error" not in tc:
                    tc["error"] = "已停止生成" if msg_status == "interrupted" else "发生错误"

    event_items.append({
        "type": "message.delta",
        "content": msg.content,
        "timestamp": _format_utc_datetime(msg.created_at),
    })

    result: dict[str, Any] = {
        "id": msg.id,
        "role": msg.role,
        "content": msg.content,
        "timestamp": _format_utc_datetime(msg.created_at),
        "events": event_items,
        "status": msg.status.value if isinstance(msg.status, MessageStatus) else msg.status,
        "isStreaming": msg.status == MessageStatus.generating,
    }
    if tool_calls:
        result["toolCalls"] = tool_calls
    if thinking:
        result["thinking"] = thinking
    if usage:
        result["usage"] = usage
    return result


class SqliteRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_agent(
        self,
        *,
        name: str,
        sandbox_type: str,
        adapter_type: str,
        workspace_path: str,
        idle_timeout_seconds: int,
        description: str = "",
        status: AgentStatus | str = AgentStatus.creating,
        sandbox_id: str | None = None,
        has_scheduled_tasks: bool = False,
        model_id: str | None = None,
        last_active_at: datetime | None = None,
    ) -> AgentRecord:
        return self.create_agent_with_id(
            agent_id=str(uuid4()),
            name=name,
            sandbox_type=sandbox_type,
            adapter_type=adapter_type,
            workspace_path=workspace_path,
            idle_timeout_seconds=idle_timeout_seconds,
            description=description,
            status=status,
            sandbox_id=sandbox_id,
            has_scheduled_tasks=has_scheduled_tasks,
            model_id=model_id,
            last_active_at=last_active_at,
        )

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
        status: AgentStatus | str = AgentStatus.creating,
        sandbox_id: str | None = None,
        has_scheduled_tasks: bool = False,
        model_id: str | None = None,
        mcp_server_list: list[str] = [],
        last_active_at: datetime | None = None,
    ) -> AgentRecord:
        with self._session_factory() as session:
            row = AgentORM(
                id=agent_id,
                name=name,
                description=description,
                sandbox_type=sandbox_type,
                adapter_type=adapter_type,
                status=self._serialize_status(status),
                sandbox_id=sandbox_id,
                workspace_path=workspace_path,
                idle_timeout_seconds=idle_timeout_seconds,
                has_scheduled_tasks=has_scheduled_tasks,
                model_id=model_id,
                mcp_server_list=mcp_server_list,
                last_active_at=last_active_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_agent_record(row)

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        with self._session_factory() as session:
            row = session.get(AgentORM, agent_id)
            if row is None:
                return None
            return self._to_agent_record(row)

    def list_agents(self) -> list[AgentRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(AgentORM)
                .filter(AgentORM.status != AgentStatus.deleted.value)
                .order_by(AgentORM.created_at.asc())
                .all()
            )
            return [self._to_agent_record(row) for row in rows]

    def list_agents_needing_recovery(self, sandbox_type: str | None = None, status_filter: list[AgentStatus] | None = None) -> list[AgentRecord]:
        """获取需要在服务重启时恢复的 agent"""
        with self._session_factory() as session:
            if status_filter is None:
                status_filter = [AgentStatus.running, AgentStatus.paused]
            query = session.query(AgentORM).filter(
                AgentORM.status.in_([s.value for s in status_filter])
            )
            if sandbox_type:
                query = query.filter(AgentORM.sandbox_type == sandbox_type)
            rows = query.order_by(AgentORM.created_at.asc()).all()
            return [self._to_agent_record(row) for row in rows]

    def list_agents_with_conversations(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            msg_count_subq = (
                session.query(
                    MessageORM.session_id,
                    func.count(MessageORM.id).label("msg_count"),
                )
                .group_by(MessageORM.session_id)
                .subquery()
            )

            last_status_subq = (
                session.query(MessageORM.status)
                .filter(
                    MessageORM.session_id == SessionORM.id,
                    MessageORM.role == "assistant",
                )
                .order_by(MessageORM.created_at.desc())
                .limit(1)
                .correlate(SessionORM)
                .scalar_subquery()
            )

            rows = (
                session.query(AgentORM, SessionORM, func.coalesce(msg_count_subq.c.msg_count, 0), last_status_subq)
                .outerjoin(SessionORM, SessionORM.agent_id == AgentORM.id)
                .outerjoin(msg_count_subq, SessionORM.id == msg_count_subq.c.session_id)
                .filter(AgentORM.status != AgentStatus.deleted.value)
                .order_by(
                    AgentORM.created_at.asc(),
                    SessionORM.updated_at.desc(),
                )
                .all()
            )

            agents_map: dict[str, dict[str, Any]] = {}
            for agent_row, session_row, msg_count, last_status in rows:
                if agent_row.id not in agents_map:
                    agents_map[agent_row.id] = {
                        "id": agent_row.id,
                        "name": agent_row.name,
                        "description": agent_row.description,
                        "sandbox_type": agent_row.sandbox_type,
                        "adapter_type": agent_row.adapter_type,
                        "status": agent_row.status,
                        "sandbox_id": agent_row.sandbox_id,
                        "workspace_path": agent_row.workspace_path,
                        "idle_timeout_seconds": agent_row.idle_timeout_seconds,
                        "has_scheduled_tasks": agent_row.has_scheduled_tasks,
                        "model_id": agent_row.model_id,
                        "created_at": agent_row.created_at,
                        "updated_at": agent_row.updated_at,
                        "conversations": [],
                    }
                if session_row is not None:
                    agents_map[agent_row.id]["conversations"].append({
                        "id": session_row.id,
                        "agent_id": session_row.agent_id,
                        "title": session_row.title,
                        "pinned": session_row.pinned,
                        "status": session_row.status.value if isinstance(session_row.status, SessionStatus) else str(session_row.status),
                        "message_count": msg_count,
                        "last_message_status": last_status.value if isinstance(last_status, MessageStatus) else last_status,
                        "created_at": session_row.created_at,
                        "updated_at": session_row.updated_at,
                    })

            return list(agents_map.values())

    def update_agent_status(
        self,
        agent_id: str,
        status: AgentStatus | str,
        updated_at: datetime | None = None,
    ) -> AgentRecord:
        with self._session_factory() as session:
            row = session.get(AgentORM, agent_id)
            if row is None:
                raise KeyError(f"Agent not found: {agent_id}")
            row.status = self._serialize_status(status)
            if updated_at is not None:
                row.updated_at = updated_at
            session.commit()
            session.refresh(row)
            return self._to_agent_record(row)

    def update_agent_mcp_server_list(
        self,
        agent_id: str,
        mcp_server_list: list[str],
    ) -> AgentRecord:
        with self._session_factory() as session:
            row = session.get(AgentORM, agent_id)
            if row is None:
                raise KeyError(f"Agent not found: {agent_id}")
            row.mcp_server_list = mcp_server_list
            session.commit()
            session.refresh(row)
            return self._to_agent_record(row)

    def create_session(
        self,
        agent_id: str,
        *,
        status: SessionStatus | str = SessionStatus.idle,
    ) -> SessionRecord:
        with self._session_factory() as session:
            row = SessionORM(id=str(uuid4()), agent_id=agent_id, status=status)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_session_record(row)

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._session_factory() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                return None
            return self._to_session_record(row)

    def find_session_by_runtime_identity(
        self,
        runtime_type: str,
        runtime_session_id: str,
    ) -> SessionRecord | None:
        with self._session_factory() as session:
            row = (
                session.query(SessionORM)
                .filter(
                    SessionORM.runtime_type == runtime_type,
                    SessionORM.runtime_session_id == runtime_session_id,
                )
                .first()
            )
            if row is None:
                return None
            return self._to_session_record(row)

    def list_sessions(self, agent_id: str) -> list[SessionRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(SessionORM)
                .filter(SessionORM.agent_id == agent_id)
                .order_by(SessionORM.created_at.asc())
                .all()
            )
            return [self._to_session_record(row) for row in rows]

    def list_sessions_by_runtime_session_ids(
        self,
        runtime_type: str,
        runtime_session_ids: list[str],
    ) -> list[SessionRecord]:
        ordered_ids = self._dedupe_preserve_order(runtime_session_ids)
        if not ordered_ids:
            return []

        with self._session_factory() as session:
            rows = (
                session.query(SessionORM)
                .filter(
                    SessionORM.runtime_type == runtime_type,
                    SessionORM.runtime_session_id.in_(ordered_ids),
                )
                .all()
            )
            by_runtime_session_id = {
                row.runtime_session_id: self._to_session_record(row)
                for row in rows
                if row.runtime_session_id is not None
            }
            return [
                by_runtime_session_id[runtime_session_id]
                for runtime_session_id in ordered_ids
                if runtime_session_id in by_runtime_session_id
            ]

    def upsert_session(
        self,
        session_id: str,
        agent_id: str,
        status: str,
        context_initialized: bool = False,
        runtime_type: str | None = None,
        runtime_session_key: str | None = None,
        created_at: datetime | None = None,
        remote_runtime_agent_id: str | None = None,
    ) -> SessionRecord:
        """Upsert session from witty-agent-server"""
        with self._session_factory() as session:
            existing = session.get(SessionORM, session_id)
            now = datetime.now(timezone.utc)

            if existing is None:
                row = SessionORM(
                    id=session_id,
                    agent_id=agent_id,
                    remote_runtime_agent_id=remote_runtime_agent_id,
                    runtime_type=runtime_type,
                    runtime_session_key=runtime_session_key,
                    status=SessionStatus(status),
                    created_at=created_at or now,
                    updated_at=now,
                )
                session.add(row)
            else:
                existing.status = SessionStatus(status)
                existing.runtime_type = runtime_type or existing.runtime_type
                existing.runtime_session_key = (
                    runtime_session_key or existing.runtime_session_key
                )
                existing.remote_runtime_agent_id = (
                    remote_runtime_agent_id or existing.remote_runtime_agent_id
                )
                existing.updated_at = now

            session.commit()
            session.refresh(existing or row)
            return self._to_session_record(existing or row)

    def update_session_runtime_identity(
        self,
        *,
        session_id: str,
        runtime_type: str,
        runtime_session_id: str,
        runtime_session_key: str,
    ) -> SessionRecord:
        with self._session_factory() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                raise session_not_found(session_id=session_id)

            row.runtime_type = runtime_type
            row.runtime_session_id = runtime_session_id
            row.runtime_session_key = runtime_session_key
            row.updated_at = datetime.now(timezone.utc)

            session.commit()
            session.refresh(row)
            return self._to_session_record(row)

    def delete_session(self, session_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                return
            session.delete(row)
            session.commit()

    def save_sandbox_state(
        self,
        agent_id: str,
        sandbox_payload_json: dict[str, Any],
        adapter_base_url: str | None,
        adapter_ready: bool = False,
        last_error: str | None = None,
    ) -> SandboxStateRecord:
        with self._session_factory() as session:
            row = session.get(AgentRuntimeStateORM, agent_id)
            if row is None:
                row = AgentRuntimeStateORM(agent_id=agent_id)
                session.add(row)
            row.runtime_payload_json = dict(sandbox_payload_json)
            row.adapter_base_url = adapter_base_url
            row.adapter_ready = adapter_ready
            row.last_error = last_error
            session.commit()
            session.refresh(row)
            return self._to_sandbox_state_record(row)

    def get_sandbox_state(self, agent_id: str) -> SandboxStateRecord | None:
        with self._session_factory() as session:
            row = session.get(AgentRuntimeStateORM, agent_id)
            if row is None:
                return None
            return self._to_sandbox_state_record(row)

    def list_runtime_session_ids_by_agent_id(
        self,
        agent_id: str,
        runtime_type: str = "openclaw",
    ) -> list[str]:
        with self._session_factory() as session:
            rows = (
                session.query(SessionORM.runtime_session_id)
                .filter(
                    SessionORM.agent_id == agent_id,
                    SessionORM.runtime_type == runtime_type,
                    SessionORM.runtime_session_id.isnot(None),
                )
                .order_by(SessionORM.created_at.asc())
                .all()
            )
            return [runtime_session_id for (runtime_session_id,) in rows]

    def list_agents_with_runtime_state(self) -> list[AgentWithRuntimeStateRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(AgentORM, AgentRuntimeStateORM)
                .outerjoin(AgentRuntimeStateORM, AgentRuntimeStateORM.agent_id == AgentORM.id)
                .filter(AgentORM.status != AgentStatus.deleted.value)
                .order_by(AgentORM.created_at.asc())
                .all()
            )
            return [
                AgentWithRuntimeStateRecord(
                    agent=self._to_agent_record(agent_row),
                    runtime_state=(
                        self._to_sandbox_state_record(runtime_state_row)
                        if runtime_state_row is not None
                        else None
                    ),
                )
                for agent_row, runtime_state_row in rows
            ]

    def list_agent_records_by_ids(self, agent_ids: list[str]) -> list[AgentRecord]:
        ordered_ids = self._dedupe_preserve_order(agent_ids)
        if not ordered_ids:
            return []

        with self._session_factory() as session:
            rows = (
                session.query(AgentORM)
                .filter(
                    AgentORM.id.in_(ordered_ids),
                    AgentORM.status != AgentStatus.deleted.value,
                )
                .all()
            )
            by_agent_id = {
                row.id: self._to_agent_record(row)
                for row in rows
            }
            return [
                by_agent_id[agent_id]
                for agent_id in ordered_ids
                if agent_id in by_agent_id
            ]

    def create_message(
        self,
        agent_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata_json: dict[str, Any] | None = None,
        status: MessageStatus = MessageStatus.completed,
    ) -> str:
        with self._session_factory() as session:
            row = MessageORM(
                id=str(uuid4()),
                agent_id=agent_id,
                session_id=session_id,
                role=role,
                content=content,
                metadata_json=dict(metadata_json or {}),
                status=status,
            )
            session.add(row)
            session.commit()
            return row.id

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
    ) -> tuple[str, int]:
        next_seq_no = seq_no
        last_error: IntegrityError | None = None

        for _ in range(max_retries):
            try:
                event_id = self._create_message_event_once(
                    agent_id=agent_id,
                    session_id=session_id,
                    event_type=event_type,
                    payload_json=payload_json,
                    seq_no=next_seq_no,
                    message_id=message_id,
                )
                return event_id, next_seq_no
            except IntegrityError as exc:
                if not self._is_message_event_seq_conflict(exc):
                    raise
                last_error = exc
                next_seq_no = self._get_next_message_event_seq(session_id=session_id)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to create message event with retry.")

    def create_assistant_message_and_bind_events(
        self,
        *,
        agent_id: str,
        session_id: str,
        content: str,
        event_ids: list[str],
        metadata_json: dict[str, Any] | None = None,
    ) -> str:
        with self._session_factory() as session:
            row = MessageORM(
                id=str(uuid4()),
                agent_id=agent_id,
                session_id=session_id,
                role="assistant",
                content=content,
                metadata_json=dict(metadata_json or {}),
            )
            session.add(row)
            session.flush()
            if event_ids:
                session.query(MessageEventORM).filter(
                    MessageEventORM.id.in_(event_ids)
                ).update(
                    {MessageEventORM.message_id: row.id},
                    synchronize_session=False,
                )
            session.commit()
            return row.id

    def update_message_content(self, message_id: str, content: str) -> None:
        with self._session_factory() as session:
            session.query(MessageORM).filter(
                MessageORM.id == message_id
            ).update(
                {MessageORM.content: content},
                synchronize_session=False,
            )
            session.commit()

    def update_message_stream_at(self, message_id: str) -> None:
        with self._session_factory() as session:
            session.query(MessageORM).filter(
                MessageORM.id == message_id
            ).update(
                {MessageORM.last_stream_at: datetime.now(timezone.utc)},
                synchronize_session=False,
            )
            session.commit()

    def update_message_status(self, message_id: str, status: MessageStatus) -> None:
        with self._session_factory() as session:
            filters = [MessageORM.id == message_id]
            if status == MessageStatus.completed:
                filters.append(MessageORM.status == MessageStatus.generating)
            session.query(MessageORM).filter(*filters).update(
                {MessageORM.status: status},
                synchronize_session=False,
            )
            session.commit()

    def find_stale_generating_messages(
        self, stale_threshold_seconds: int
    ) -> list[MessageORM]:
        from datetime import timedelta

        threshold = datetime.now(timezone.utc) - timedelta(
            seconds=stale_threshold_seconds
        )
        with self._session_factory() as session:
            return (
                session.query(MessageORM)
                .filter(
                    MessageORM.status == MessageStatus.generating,
                    MessageORM.last_stream_at < threshold,
                )
                .all()
            )

    def find_last_assistant_message_for_session(self, session_id: str) -> MessageORM | None:
        with self._session_factory() as session:
            return (
                session.query(MessageORM)
                .filter(
                    MessageORM.session_id == session_id,
                    MessageORM.role == "assistant",
                )
                .order_by(MessageORM.created_at.desc())
                .first()
            )

    def find_generating_message_for_session(self, session_id: str) -> MessageORM | None:
        with self._session_factory() as session:
            return (
                session.query(MessageORM)
                .filter(
                    MessageORM.session_id == session_id,
                    MessageORM.role == "assistant",
                    MessageORM.status == MessageStatus.generating,
                )
                .order_by(MessageORM.created_at.desc())
                .first()
            )

    def compact_message_delta_events(self, message_id: str) -> None:
        from sqlalchemy import select

        BATCH = 500
        with self._session_factory() as session:
            while True:
                subq = select(MessageEventORM.id).filter(
                    MessageEventORM.message_id == message_id,
                    MessageEventORM.event_type.in_(["message.delta", "thinking.delta"]),
                ).limit(BATCH)
                deleted = (
                    session.query(MessageEventORM)
                    .filter(MessageEventORM.id.in_(subq))
                    .delete(synchronize_session=False)
                )
                if deleted == 0:
                    break
                session.commit()

    def _get_next_message_event_seq(self, *, session_id: str) -> int:
        with self._session_factory() as session:
            max_seq_no = (
                session.query(func.max(MessageEventORM.seq_no))
                .filter(MessageEventORM.session_id == session_id)
                .scalar()
            )
            return int(max_seq_no or 0) + 1

    def _create_message_event_once(
        self,
        *,
        agent_id: str,
        session_id: str,
        event_type: str,
        payload_json: dict[str, Any],
        seq_no: int,
        message_id: str | None = None,
    ) -> str:
        with self._session_factory() as session:
            row = MessageEventORM(
                id=str(uuid4()),
                agent_id=agent_id,
                session_id=session_id,
                message_id=message_id,
                event_type=event_type,
                payload_json=dict(payload_json),
                seq_no=seq_no,
            )
            session.add(row)
            session.commit()
            return row.id

    @staticmethod
    def _is_message_event_seq_conflict(exc: IntegrityError) -> bool:
        message = str(exc.orig if exc.orig is not None else exc)
        return (
            "uq_message_events_session_seq" in message
            or "message_events.session_id, message_events.seq_no" in message
        )

    @staticmethod
    def _dedupe_preserve_order(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def get_message_count(self, session_id: str) -> int:
        with self._session_factory() as session:
            return (
                session.query(func.count(MessageORM.id))
                .filter(MessageORM.session_id == session_id)
                .scalar()
            ) or 0

    def get_last_assistant_status(self, session_id: str) -> str | None:
        with self._session_factory() as session:
            row = (
                session.query(MessageORM)
                .filter(
                    MessageORM.session_id == session_id,
                    MessageORM.role == "assistant",
                )
                .order_by(MessageORM.created_at.desc())
                .first()
            )
            if row is None:
                return None
            return row.status.value if isinstance(row.status, MessageStatus) else row.status

    def get_first_user_message(self, session_id: str) -> str | None:
        with self._session_factory() as session:
            row = (
                session.query(MessageORM)
                .filter(MessageORM.session_id == session_id, MessageORM.role == "user")
                .order_by(MessageORM.created_at.asc())
                .first()
            )
            if row is None:
                return None
            return row.content

    def update_session_metadata(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> SessionRecord:
        with self._session_factory() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                raise KeyError(f"Session not found: {session_id}")
            if title is not None:
                row.title = title
            if pinned is not None:
                row.pinned = pinned
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return self._to_session_record(row)

    def list_sessions_with_summary(self, agent_id: str) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            msg_count_subq = (
                session.query(
                    MessageORM.session_id,
                    func.count(MessageORM.id).label("msg_count"),
                )
                .group_by(MessageORM.session_id)
                .subquery()
            )

            last_status_subq = (
                session.query(MessageORM.status)
                .filter(
                    MessageORM.session_id == SessionORM.id,
                    MessageORM.role == "assistant",
                )
                .order_by(MessageORM.created_at.desc())
                .limit(1)
                .correlate(SessionORM)
                .scalar_subquery()
            )

            rows = (
                session.query(
                    SessionORM,
                    func.coalesce(msg_count_subq.c.msg_count, 0),
                    last_status_subq,
                )
                .outerjoin(msg_count_subq, SessionORM.id == msg_count_subq.c.session_id)
                .filter(SessionORM.agent_id == agent_id)
                .order_by(SessionORM.updated_at.desc())
                .all()
            )
            result = []
            for row, msg_count, last_status in rows:
                result.append({
                    "id": row.id,
                    "agent_id": row.agent_id,
                    "title": row.title,
                    "pinned": row.pinned,
                    "status": row.status.value if isinstance(row.status, SessionStatus) else row.status,
                    "message_count": msg_count,
                    "last_message_status": last_status.value if isinstance(last_status, MessageStatus) else last_status,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                })
            return result

    def get_messages_with_events(
        self, session_id: str, limit: int = 20, before: str | None = None
    ) -> tuple[list[dict[str, Any]], bool]:
        with self._session_factory() as session:
            query = session.query(MessageORM).filter(
                MessageORM.session_id == session_id
            )
            if before is not None:
                cursor_dt = datetime.fromisoformat(before)
                query = query.filter(MessageORM.created_at < cursor_dt)

            messages = (
                query.order_by(MessageORM.created_at.desc())
                .limit(limit + 1)
                .all()
            )

            has_more = len(messages) > limit
            messages = messages[:limit]
            messages.reverse()

            if not messages:
                return [], False

            message_ids = [m.id for m in messages]
            all_events = (
                session.query(MessageEventORM)
                .filter(MessageEventORM.message_id.in_(message_ids))
                .order_by(MessageEventORM.seq_no.asc())
                .all()
            )
            events_by_message: dict[str, list[MessageEventORM]] = {}
            for evt in all_events:
                if evt.message_id not in events_by_message:
                    events_by_message[evt.message_id] = []
                events_by_message[evt.message_id].append(evt)

            result = []
            for msg in messages:
                evt_rows = events_by_message.pop(msg.id, [])
                result.append(_assemble_message(msg, evt_rows))
            return result, has_more

    def delete_agent(self, agent_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(AgentORM, agent_id)
            if row is None:
                return
            builtin_skill_ids = [
                skill_id
                for (skill_id,) in (
                    session.query(AgentSkillORM.skill_id)
                    .filter(
                        AgentSkillORM.agent_id == agent_id,
                        AgentSkillORM.source_type == 'builtin',
                    )
                    .all()
                )
            ]
            session.delete(row)
            session.flush()
            for skill_id in builtin_skill_ids:
                skill_row = session.get(SkillORM, skill_id)
                if skill_row is not None:
                    session.delete(skill_row)
            session.commit()

    @staticmethod
    def _serialize_status(status: AgentStatus | str) -> str:
        return (
            status.value
            if isinstance(status, AgentStatus)
            else AgentStatus(status).value
        )

    @staticmethod
    def _to_agent_record(row: AgentORM) -> AgentRecord:
        return AgentRecord(
            id=row.id,
            name=row.name,
            description=row.description,
            sandbox_type=row.sandbox_type,
            adapter_type=row.adapter_type,
            status=AgentStatus(row.status),
            sandbox_id=row.sandbox_id,
            workspace_path=row.workspace_path,
            idle_timeout_seconds=row.idle_timeout_seconds,
            has_scheduled_tasks=row.has_scheduled_tasks,
            model_id=row.model_id,
            mcp_server_list=row.mcp_server_list or [],
            last_active_at=row.last_active_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_session_record(row: SessionORM) -> SessionRecord:
        return SessionRecord(
            id=row.id,
            agent_id=row.agent_id,
            remote_runtime_agent_id=row.remote_runtime_agent_id,
            status=row.status.value
            if isinstance(row.status, SessionStatus)
            else row.status,
            runtime_type=row.runtime_type,
            runtime_session_id=row.runtime_session_id,
            runtime_session_key=row.runtime_session_key,
            title=row.title,
            pinned=row.pinned,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_sandbox_state_record(row: AgentRuntimeStateORM) -> SandboxStateRecord:
        return SandboxStateRecord(
            agent_id=row.agent_id,
            sandbox_payload_json=dict(row.runtime_payload_json or {}),
            adapter_base_url=row.adapter_base_url,
            adapter_ready=row.adapter_ready,
            last_error=row.last_error,
        )

    def create_model(
        self,
        *,
        name: str,
        provider: str,
        api_key: str,
        api_base_url: str | None = None,
        compatibility: str | None = None,
        enabled: bool = True,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        is_default: bool = False,
    ) -> ModelRecord:
        return self.create_model_with_id(
            model_id=str(uuid4()),
            name=name,
            provider=provider,
            api_key=api_key,
            api_base_url=api_base_url,
            compatibility=compatibility,
            enabled=enabled,
            max_tokens=max_tokens,
            temperature=temperature,
            is_default=is_default,
        )

    def create_model_with_id(
        self,
        *,
        model_id: str,
        name: str,
        provider: str,
        api_key: str,
        api_base_url: str | None = None,
        compatibility: str | None = None,
        enabled: bool = True,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        is_default: bool = False,
    ) -> ModelRecord:
        with self._session_factory() as session:
            row = ModelORM(
                id=model_id,
                name=name,
                provider=provider,
                api_key=api_key,
                api_base_url=api_base_url,
                compatibility=compatibility,
                enabled=enabled,
                max_tokens=max_tokens,
                temperature=temperature,
                is_default=is_default,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_model_record(row)

    def list_models(self) -> list[ModelRecord]:
        with self._session_factory() as session:
            rows = session.query(ModelORM).order_by(ModelORM.created_at.asc()).all()
            return [self._to_model_record(row) for row in rows]

    def get_model(self, model_id: str) -> ModelRecord | None:
        with self._session_factory() as session:
            row = session.get(ModelORM, model_id)
            if row is None:
                return None
            return self._to_model_record(row)

    def delete_model(self, model_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(ModelORM, model_id)
            if row is None:
                return
            session.delete(row)
            session.commit()

    def update_model(
        self,
        model_id: str,
        *,
        name: str | None = None,
        provider: str | None = None,
        api_key: str | None = None,
        api_base_url: str | None = None,
        compatibility: str | None = None,
        enabled: bool | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        is_default: bool | None = None,
    ) -> ModelRecord:
        with self._session_factory() as session:
            row = session.get(ModelORM, model_id)
            if row is None:
                raise KeyError(f"Model not found: {model_id}")
            if name is not None:
                row.name = name
            if provider is not None:
                row.provider = provider
            if api_key is not None:
                row.api_key = api_key
            if api_base_url is not None:
                row.api_base_url = api_base_url
            if compatibility is not None:
                row.compatibility = compatibility
            if enabled is not None:
                row.enabled = enabled
            if max_tokens is not None:
                row.max_tokens = max_tokens
            if temperature is not None:
                row.temperature = temperature
            if is_default is not None:
                row.is_default = is_default
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return self._to_model_record(row)

    @staticmethod
    def _to_model_record(row: ModelORM) -> ModelRecord:
        return ModelRecord(
            id=row.id,
            name=row.name,
            provider=row.provider,
            api_key=row.api_key,
            api_base_url=row.api_base_url,
            compatibility=row.compatibility,
            enabled=row.enabled,
            max_tokens=row.max_tokens,
            temperature=row.temperature,
            is_default=row.is_default,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def create_mcp_server(
        self,
        *,
        mcp_server_name: str,
        mcp_server_config: dict[str, Any],
    ) -> McpServerRecord:
        return self.create_mcp_server_with_id(
            server_id=str(uuid4()),
            mcp_server_name=mcp_server_name,
            mcp_server_config=mcp_server_config,
        )

    def create_mcp_server_with_id(
        self,
        *,
        server_id: str,
        mcp_server_name: str,
        mcp_server_config: dict[str, Any],
    ) -> McpServerRecord:
        with self._session_factory() as session:
            row = McpServerORM(
                id=server_id,
                mcp_server_name=mcp_server_name,
                mcp_server_config=mcp_server_config,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_mcp_server_record(row)

    def list_mcp_servers(self) -> list[McpServerRecord]:
        with self._session_factory() as session:
            rows = session.query(McpServerORM).order_by(McpServerORM.created_at.asc()).all()
            return [self._to_mcp_server_record(row) for row in rows]

    def get_mcp_server(self, server_id: str) -> McpServerRecord | None:
        with self._session_factory() as session:
            row = session.get(McpServerORM, server_id)
            if row is None:
                return None
            return self._to_mcp_server_record(row)

    def delete_mcp_server(self, server_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(McpServerORM, server_id)
            if row is None:
                return
            session.delete(row)
            session.commit()

    def update_mcp_server(
        self,
        server_id: str,
        *,
        mcp_server_name: str | None = None,
        mcp_server_config: dict[str, Any] | None = None,
    ) -> McpServerRecord:
        with self._session_factory() as session:
            row = session.get(McpServerORM, server_id)
            if row is None:
                raise KeyError(f"MCP Server not found: {server_id}")
            if mcp_server_name is not None:
                row.mcp_server_name = mcp_server_name
            if mcp_server_config is not None:
                row.mcp_server_config = mcp_server_config
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return self._to_mcp_server_record(row)

    @staticmethod
    def _to_mcp_server_record(row: McpServerORM) -> McpServerRecord:
        return McpServerRecord(
            id=row.id,
            mcp_server_name=row.mcp_server_name,
            mcp_server_config=row.mcp_server_config,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def list_skill_repositories(self) -> list[SkillRepositoryRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(SkillRepositoryORM)
                .order_by(
                    SkillRepositoryORM.created_at.desc(),
                    SkillRepositoryORM.repo_name.asc(),
                )
                .all()
            )
            return [self._to_skill_repository_record(row) for row in rows]

    def get_skill_repository(self, repo_id: str) -> SkillRepositoryRecord | None:
        with self._session_factory() as session:
            row = session.get(SkillRepositoryORM, repo_id)
            if row is None:
                return None
            return self._to_skill_repository_record(row)

    def get_skill_repository_by_name(self, name: str) -> SkillRepositoryRecord | None:
        with self._session_factory() as session:
            row = (
                session.query(SkillRepositoryORM)
                .filter(SkillRepositoryORM.repo_name == name)
                .one_or_none()
            )
            if row is None:
                return None
            return self._to_skill_repository_record(row)

    def create_skill_repository(
        self,
        *,
        name: str,
        source_type: str,
        branch: str | None,
        url: str | None,
        local_path: str | None,
        skill_discover_status: str | None,
    ) -> SkillRepositoryRecord:
        with self._session_factory() as session:
            row = SkillRepositoryORM(
                repo_id=str(uuid4()),
                repo_name=name,
                source_type=source_type,
                branch=branch,
                url=url,
                local_path=local_path,
                skill_discover_status=skill_discover_status,
                skill_num=0,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_skill_repository_record(row)

    def update_skill_repository(
        self,
        repo_id: str,
        **updates: Any,
    ) -> SkillRepositoryRecord:
        with self._session_factory() as session:
            row = session.get(SkillRepositoryORM, repo_id)
            if row is None:
                raise KeyError(f'Skill repository not found: {repo_id}')
            for key, value in updates.items():
                setattr(row, key, value)
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return self._to_skill_repository_record(row)

    def delete_skill_repository(self, repo_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(SkillRepositoryORM, repo_id)
            if row is None:
                return
            session.delete(row)
            session.commit()

    def get_skill_by_skill_id(self, skill_id: str) -> SkillRecord | None:
        with self._session_factory() as session:
            row = (
                session.query(SkillORM)
                .filter(SkillORM.skill_id == skill_id)
                .one_or_none()
            )
            if row is None:
                return None
            return self._to_skill_record(row)

    def list_skills(self) -> list[SkillRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(SkillORM)
                .filter(SkillORM.repo_id.is_not(None))
                .order_by(SkillORM.created_at.asc(), SkillORM.skill_name.asc())
                .all()
            )
            return [self._to_skill_record(row) for row in rows]

    def update_skills(
        self,
        repo_id: str,
        skills: list[SkillRecord],
    ) -> None:
        with self._session_factory() as session:
            repository = session.get(SkillRepositoryORM, repo_id)
            if repository is None:
                raise KeyError(f'Skill repository not found: {repo_id}')

            session.query(SkillORM).filter(SkillORM.repo_id == repo_id).delete(
                synchronize_session=False
            )

            for item in skills:
                skill_id = item.skill_id or str(uuid4())
                row = SkillORM(
                    skill_id=skill_id,
                    repo_id=repo_id,
                    skill_name=item.skill_name or '',
                    relative_path=item.relative_path,
                    metadata_json=dict(item.metadata or {}),
                    skill_source=item.skill_source,
                    skill_md_url=item.skill_md_url,
                )
                session.add(row)

            session.commit()

    def upsert_builtin_skill(
        self,
        *,
        skill_id: str,
        skill_name: str,
        metadata: dict[str, Any],
        skill_source: str | None = None,
        relative_path: str | None = None,
    ) -> SkillRecord:
        with self._session_factory() as session:
            row = session.get(SkillORM, skill_id)
            if row is None:
                row = SkillORM(
                    skill_id=skill_id,
                    repo_id=None,
                    skill_name=skill_name,
                    relative_path=relative_path,
                    metadata_json=dict(metadata),
                    skill_source=skill_source,
                    skill_md_url=None,
                )
                session.add(row)
            else:
                row.repo_id = None
                row.skill_name = skill_name
                row.relative_path = relative_path
                row.metadata_json = dict(metadata)
                row.skill_source = skill_source
                row.skill_md_url = None
            session.commit()
            session.refresh(row)
            return self._to_skill_record(row)

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
    ) -> AgentSkillRecord:
        with self._session_factory() as session:
            row = session.get(AgentSkillORM, (agent_id, skill_id))
            timestamp = installed_at or datetime.now(timezone.utc)
            if row is None:
                row = AgentSkillORM(
                    agent_id=agent_id,
                    skill_id=skill_id,
                    source_type=source_type,
                    repo_id=repo_id,
                    skill_name=skill_name,
                    relative_path=relative_path,
                    metadata_json=dict(metadata) if metadata else None,
                    skill_source=skill_source,
                    skill_md_url=skill_md_url,
                    installed_at=timestamp,
                )
                session.add(row)
            else:
                row.source_type = source_type
                row.repo_id = repo_id
                row.skill_name = skill_name
                row.relative_path = relative_path
                row.metadata_json = dict(metadata) if metadata else None
                row.skill_source = skill_source
                row.skill_md_url = skill_md_url
                row.installed_at = timestamp
            session.commit()
            session.refresh(row)
            return self._to_agent_skill_record(row)

    def replace_installed_agent_skills_from_runtime(
        self,
        *,
        agent_id: str,
        skills: list[dict[str, Any]],
    ) -> None:
        """Reconcile runtime-visible skills with persisted install records."""
        with self._session_factory() as session:
            timestamp = datetime.now(timezone.utc)
            normalized_skills: list[dict[str, Any]] = []
            seen_skills: set[tuple[str, str | None]] = set()
            for item in skills:
                if not isinstance(item, dict):
                    continue
                raw_name = item.get("name")
                if not isinstance(raw_name, str):
                    continue
                skill_name = raw_name.strip()
                raw_file_path = item.get("filePath")
                relative_path = (
                    raw_file_path.strip()
                    if isinstance(raw_file_path, str) and raw_file_path.strip()
                    else None
                )
                skill_key = (skill_name, relative_path)
                if not skill_name or skill_key in seen_skills:
                    continue
                seen_skills.add(skill_key)
                normalized_skills.append(
                    {
                        "skill_id": str(
                            uuid5(
                                NAMESPACE_URL,
                                f"builtin:{agent_id}:{skill_name}:{relative_path or ''}",
                            )
                        ),
                        "skill_name": skill_name,
                        "metadata": dict(item),
                        "skill_source": item.get("source")
                        if isinstance(item.get("source"), str)
                        else None,
                        "relative_path": relative_path,
                    }
                )

            installed_rows = (
                session.query(AgentSkillORM)
                .filter(AgentSkillORM.agent_id == agent_id)
                .all()
            )
            incoming_keys = {
                (item["skill_name"], item["relative_path"])
                for item in normalized_skills
            }
            non_builtin_keys = {
                (
                    row.skill_name.strip(),
                    row.relative_path.strip() if row.relative_path else None,
                )
                for row in installed_rows
                if row.source_type != 'builtin'
            }
            obsolete_builtin_rows = [
                row
                for row in installed_rows
                if row.source_type == 'builtin'
                and (
                    (row.skill_name.strip(), row.relative_path) not in incoming_keys
                    or (row.skill_name.strip(), row.relative_path) in non_builtin_keys
                )
            ]
            obsolete_builtin_ids = [row.skill_id for row in obsolete_builtin_rows]
            for row in obsolete_builtin_rows:
                session.delete(row)
            if obsolete_builtin_ids:
                session.query(SkillORM).filter(SkillORM.skill_id.in_(obsolete_builtin_ids)).delete(
                    synchronize_session=False
                )

            existing_keys = {
                (
                    row.skill_name.strip(),
                    row.relative_path.strip() if row.relative_path else None,
                )
                for row in installed_rows
                if row not in obsolete_builtin_rows
            }
            for item in normalized_skills:
                skill_key = (item["skill_name"], item["relative_path"])
                if skill_key in existing_keys:
                    continue
                skill_row = session.get(SkillORM, item["skill_id"])
                if skill_row is None:
                    skill_row = SkillORM(
                        skill_id=item["skill_id"],
                        repo_id=None,
                        skill_name=item["skill_name"],
                        relative_path=item["relative_path"],
                        metadata_json=item["metadata"],
                        skill_source=item["skill_source"],
                        skill_md_url=None,
                    )
                    session.add(skill_row)
                else:
                    skill_row.repo_id = None
                    skill_row.skill_name = item["skill_name"]
                    skill_row.relative_path = item["relative_path"]
                    skill_row.metadata_json = item["metadata"]
                    skill_row.skill_source = item["skill_source"]
                    skill_row.skill_md_url = None
                    skill_row.updated_at = timestamp

                session.add(
                    AgentSkillORM(
                        agent_id=agent_id,
                        skill_id=item["skill_id"],
                        source_type='builtin',
                        repo_id=None,
                        skill_name=item["skill_name"],
                        relative_path=item["relative_path"],
                        metadata_json=item["metadata"],
                        skill_source=item["skill_source"],
                        skill_md_url=None,
                        installed_at=timestamp,
                    )
                )
                existing_keys.add(skill_key)

            session.commit()

    def list_installed_agent_skills(self, agent_id: str) -> list[AgentSkillRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(AgentSkillORM)
                .filter(AgentSkillORM.agent_id == agent_id)
                .order_by(AgentSkillORM.installed_at.asc(), AgentSkillORM.skill_name.asc())
                .all()
            )
            return [
                self._to_agent_skill_record(row)
                for row in rows
            ]

    def get_installed_agent_skill(
        self,
        *,
        agent_id: str,
        skill_id: str,
    ) -> AgentSkillRecord | None:
        with self._session_factory() as session:
            row = session.get(AgentSkillORM, (agent_id, skill_id))
            if row is None:
                return None
            return self._to_agent_skill_record(row)

    def delete_installed_agent_skill(self, *, agent_id: str, skill_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(AgentSkillORM, (agent_id, skill_id))
            is_builtin_skill = row is not None and row.source_type == 'builtin'
            if row is None:
                return
            session.delete(row)
            session.flush()
            if is_builtin_skill:
                # 只有builtin的skill，需要同 skills 表里的那条 builtin skill 元数据一起清理
                skill_row = session.get(SkillORM, skill_id)
                if skill_row is not None:
                    session.delete(skill_row)
            session.commit()

    @staticmethod
    def _to_skill_repository_record(row: SkillRepositoryORM) -> SkillRepositoryRecord:
        return SkillRepositoryRecord(
            repo_id=row.repo_id,
            repo_name=row.repo_name,
            source_type=row.source_type,
            branch=row.branch,
            url=row.url,
            local_path=row.local_path,
            skill_discover_status=row.skill_discover_status,
            skill_num=row.skill_num,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_skill_record(row: SkillORM) -> SkillRecord:
        return SkillRecord(
            skill_id=row.skill_id,
            repo_id=row.repo_id,
            skill_name=row.skill_name,
            relative_path=row.relative_path,
            metadata=dict(row.metadata_json or {}),
            skill_source=row.skill_source,
            skill_md_url=row.skill_md_url,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_agent_skill_record(
        row: AgentSkillORM,
    ) -> AgentSkillRecord:
        return AgentSkillRecord(
            agent_id=row.agent_id,
            skill_id=row.skill_id,
            source_type=row.source_type,
            repo_id=row.repo_id,
            skill_name=row.skill_name,
            installed_at=row.installed_at,
            relative_path=row.relative_path,
            metadata=dict(row.metadata_json or {}) if row.metadata_json else None,
            skill_source=row.skill_source,
            skill_md_url=row.skill_md_url,
        )

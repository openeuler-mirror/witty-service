from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from witty_service.application.insight_health_matcher import HealthMatcher
from witty_service.domain.errors import (
    DomainError,
    insight_bad_response,
    insight_session_mapping_not_found,
    insight_timeout,
    insight_unavailable,
    insight_upstream_error,
)
from witty_service.persistence.repositories import SessionRecord

if TYPE_CHECKING:
    from witty_service.api.services import ServiceContainer


logger = logging.getLogger(__name__)

_RUNTIME_TYPE = "opencode"
_INTERRUPTION_SEVERITIES = ("critical", "high", "medium", "low")


class InsightFacade:
    def __init__(self, services: ServiceContainer) -> None:
        self._services = services
        self._repository = services.repository
        self._health_matcher = HealthMatcher()

    async def get_capabilities(self) -> dict[str, Any]:
        enabled = self._services.insight_http_client is not None
        if not enabled:
            return {
                "enabled": False,
                "reachable": False,
                "features": self._feature_flags(False),
            }

        try:
            await self._insight_get_json("/health")
        except DomainError:
            reachable = False
        else:
            reachable = True

        return {
            "enabled": True,
            "reachable": reachable,
            "features": self._feature_flags(True),
        }

    async def list_witty_agents(self) -> list[dict[str, Any]]:
        return [
            {
                "witty_agent_id": agent.id,
                "witty_agent_name": agent.name,
                "status": agent.status.value,
            }
            for agent in self._repository.list_agents()
        ]

    async def list_sessions(
        self,
        *,
        witty_agent_id: str | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> list[dict[str, Any]]:
        runtime_session_ids = self._list_managed_runtime_session_ids(
            witty_agent_id=witty_agent_id
        )
        if not runtime_session_ids:
            return []

        raw_sessions = await self._insight_get_json(
            "/api/sessions",
            params=self._raw_params(
                start_ns=start_ns,
                end_ns=end_ns,
                session_ids=runtime_session_ids,
            ),
        )
        if not isinstance(raw_sessions, list):
            return []

        sessions_by_runtime_id = {
            session.runtime_session_id: session
            for session in self._repository.list_sessions_by_runtime_session_ids(
                _RUNTIME_TYPE,
                runtime_session_ids,
            )
            if session.runtime_session_id is not None
        }
        agent_ids = self._dedupe_preserve_order(
            [session.agent_id for session in sessions_by_runtime_id.values()]
        )
        agents_by_id = {
            agent.id: agent
            for agent in self._repository.list_agent_records_by_ids(agent_ids)
        }

        enriched: list[dict[str, Any]] = []
        for item in raw_sessions:
            if not isinstance(item, dict):
                continue
            runtime_session_id = item.get("session_id")
            if not isinstance(runtime_session_id, str):
                continue

            session = sessions_by_runtime_id.get(runtime_session_id)
            if session is None:
                logger.warning(
                    "runtime session is missing a local witty session mapping: runtime_session_id=%s",
                    runtime_session_id,
                )
                continue

            agent = agents_by_id.get(session.agent_id)
            if agent is None:
                logger.warning(
                    "mapped witty session references a missing agent: session_id=%s runtime_session_id=%s agent_id=%s",
                    session.id,
                    runtime_session_id,
                    session.agent_id,
                )
                continue

            enriched.append(
                {
                    "session_id": session.id,
                    "runtime_session_id": runtime_session_id,
                    "witty_agent_id": agent.id,
                    "witty_agent_name": agent.name,
                    "agent_name": agent.name,
                    "conversation_count": int(item.get("conversation_count", 0) or 0),
                    "first_seen_ns": int(item.get("first_seen_ns", 0) or 0),
                    "last_seen_ns": int(item.get("last_seen_ns", 0) or 0),
                    "total_input_tokens": int(item.get("total_input_tokens", 0) or 0),
                    "total_output_tokens": int(item.get("total_output_tokens", 0) or 0),
                    "model": item.get("model"),
                }
            )
        return enriched

    async def get_session_traces(
        self,
        witty_session_id: str,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> Any:
        session = self._require_runtime_session(witty_session_id)
        return await self._insight_get_json(
            f"/api/sessions/{session.runtime_session_id}/traces",
            params=self._raw_params(start_ns=start_ns, end_ns=end_ns),
        )

    async def get_session_interruptions(
        self, witty_session_id: str
    ) -> list[dict[str, Any]]:
        session = self._require_runtime_session(witty_session_id)
        result = await self._insight_get_json(
            f"/api/sessions/{session.runtime_session_id}/interruptions"
        )
        if not isinstance(result, list):
            return []
        return self._remap_interruption_records(result)

    async def get_conversation_interruptions(
        self, conversation_id: str
    ) -> list[dict[str, Any]]:
        result = await self._insight_get_json(
            f"/api/conversations/{conversation_id}/interruptions"
        )
        if not isinstance(result, list):
            return []
        return self._remap_interruption_records(result)

    async def get_trace_detail(self, trace_id: str) -> Any:
        return await self._insight_get_json(f"/api/traces/{trace_id}")

    async def get_conversation_detail(self, conversation_id: str) -> Any:
        return await self._insight_get_json(f"/api/conversations/{conversation_id}")

    async def resolve_interruption(self, interruption_id: str) -> dict[str, Any]:
        result = await self._insight_post_json(
            f"/api/interruptions/{interruption_id}/resolve"
        )
        return result if isinstance(result, dict) else {"status": "resolved"}

    async def get_timeseries(
        self,
        *,
        witty_agent_id: str | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
        buckets: int | None = None,
    ) -> dict[str, Any]:
        runtime_session_ids = self._list_managed_runtime_session_ids(
            witty_agent_id=witty_agent_id
        )
        if not runtime_session_ids:
            return {"token_series": [], "model_series": []}
        result = await self._insight_get_json(
            "/api/timeseries",
            params=self._raw_params(
                start_ns=start_ns,
                end_ns=end_ns,
                buckets=buckets,
                session_ids=runtime_session_ids,
            ),
        )
        return (
            result
            if isinstance(result, dict)
            else {"token_series": [], "model_series": []}
        )

    async def get_interruption_count(
        self,
        *,
        witty_agent_id: str | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> dict[str, Any]:
        runtime_session_ids = self._list_managed_runtime_session_ids(
            witty_agent_id=witty_agent_id
        )
        if not runtime_session_ids:
            return self._empty_interruption_count()
        result = await self._insight_get_json(
            "/api/interruptions/count",
            params=self._raw_params(
                start_ns=start_ns,
                end_ns=end_ns,
                session_ids=runtime_session_ids,
            ),
        )
        return result if isinstance(result, dict) else self._empty_interruption_count()

    async def get_interruption_stats(
        self,
        *,
        witty_agent_id: str | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> list[dict[str, Any]]:
        runtime_session_ids = self._list_managed_runtime_session_ids(
            witty_agent_id=witty_agent_id
        )
        if not runtime_session_ids:
            return []
        result = await self._insight_get_json(
            "/api/interruptions/stats",
            params=self._raw_params(
                start_ns=start_ns,
                end_ns=end_ns,
                session_ids=runtime_session_ids,
            ),
        )
        return result if isinstance(result, list) else []

    async def get_interruption_session_counts(
        self,
        *,
        witty_agent_id: str | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> list[dict[str, Any]]:
        runtime_session_ids = self._list_managed_runtime_session_ids(
            witty_agent_id=witty_agent_id
        )
        if not runtime_session_ids:
            return []

        result = await self._insight_get_json(
            "/api/interruptions/session-counts",
            params=self._raw_params(
                start_ns=start_ns,
                end_ns=end_ns,
                session_ids=runtime_session_ids,
            ),
        )
        if not isinstance(result, list):
            return []

        session_by_runtime_id = {
            session.runtime_session_id: session
            for session in self._repository.list_sessions_by_runtime_session_ids(
                _RUNTIME_TYPE,
                [
                    item.get("session_id")
                    for item in result
                    if isinstance(item, dict)
                    and isinstance(item.get("session_id"), str)
                ],
            )
            if session.runtime_session_id is not None
        }

        remapped: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, dict):
                continue
            runtime_session_id = item.get("session_id")
            if not isinstance(runtime_session_id, str):
                continue
            session = session_by_runtime_id.get(runtime_session_id)
            if session is None:
                continue
            remapped.append(
                {
                    "session_id": session.id,
                    "runtime_session_id": runtime_session_id,
                    "total": int(item.get("total", 0) or 0),
                    "by_severity": self._normalize_severity_counts(
                        item.get("by_severity")
                    ),
                    "types": item.get("types")
                    if isinstance(item.get("types"), list)
                    else [],
                }
            )
        return remapped

    async def get_interruption_conversation_counts(
        self,
        *,
        witty_agent_id: str | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> list[dict[str, Any]]:
        runtime_session_ids = self._list_managed_runtime_session_ids(
            witty_agent_id=witty_agent_id
        )
        if not runtime_session_ids:
            return []
        result = await self._insight_get_json(
            "/api/interruptions/conversation-counts",
            params=self._raw_params(
                start_ns=start_ns,
                end_ns=end_ns,
                session_ids=runtime_session_ids,
            ),
        )
        return result if isinstance(result, list) else []

    async def delete_agent_health(self, pid: int) -> dict[str, Any]:
        result = await self._insight_delete_json(f"/api/agent-health/{pid}")
        return result if isinstance(result, dict) else {"ok": True}

    async def restart_agent_health(self, pid: int) -> dict[str, Any]:
        result = await self._insight_post_json(f"/api/agent-health/{pid}/restart")
        return (
            result
            if isinstance(result, dict)
            else {"ok": True, "new_pid": 0, "cmd": []}
        )

    async def export_atif_session(self, witty_session_id: str) -> dict[str, Any]:
        session = self._require_runtime_session(witty_session_id)
        result = await self._insight_get_json(
            f"/api/export/atif/session/{session.runtime_session_id}"
        )
        if not isinstance(result, dict):
            return {}
        document = dict(result)
        document["session_id"] = session.id
        document["runtime_session_id"] = session.runtime_session_id
        return document

    async def export_atif_conversation(self, conversation_id: str) -> dict[str, Any]:
        result = await self._insight_get_json(
            f"/api/export/atif/conversation/{conversation_id}"
        )
        return result if isinstance(result, dict) else {}

    async def get_agent_health(self) -> dict[str, Any]:
        raw_result = await self._insight_get_json("/api/agent-health")
        raw_agents = raw_result.get("agents") if isinstance(raw_result, dict) else []
        runtimes = [
            self._health_matcher.normalize_runtime_health(item)
            for item in raw_agents
            if isinstance(item, dict)
        ]
        managed_records = self._repository.list_agents_with_runtime_state()

        agents: list[dict[str, Any]] = []
        matched_runtime_pids: set[int] = set()
        for record in managed_records:
            candidates = self._health_matcher.find_runtime_candidates(record, runtimes)
            matched_runtime_pids.update(
                runtime["pid"]
                for runtime in candidates
                if isinstance(runtime.get("pid"), int)
            )
            agents.append(
                self._health_matcher.build_managed_health_entry(record, candidates)
            )

        orphan_runtimes = [
            runtime
            for runtime in runtimes
            if not isinstance(runtime.get("pid"), int)
            or runtime["pid"] not in matched_runtime_pids
        ]
        return {
            "agents": agents,
            "orphan_runtimes": orphan_runtimes,
            "last_scan_time": int(raw_result.get("last_scan_time", 0) or 0)
            if isinstance(raw_result, dict)
            else 0,
        }

    async def _insight_get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        client = self._services.get_insight_http_client()
        try:
            return await client.get(path, params=params)
        except httpx.ConnectError as exc:
            raise insight_unavailable(
                base_url=client.base_url, path=path, reason=str(exc)
            ) from exc
        except httpx.TimeoutException as exc:
            raise insight_timeout(
                base_url=client.base_url,
                path=path,
                timeout_seconds=client._timeout,
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise insight_upstream_error(
                base_url=client.base_url,
                path=path,
                status_code=exc.response.status_code,
                response_text=exc.response.text,
            ) from exc
        except ValueError as exc:
            raise insight_bad_response(
                base_url=client.base_url, path=path, reason=str(exc)
            ) from exc

    async def _insight_post_json(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        client = self._services.get_insight_http_client()
        try:
            return await client.post(path, json=json, timeout=timeout)
        except httpx.ConnectError as exc:
            raise insight_unavailable(
                base_url=client.base_url, path=path, reason=str(exc)
            ) from exc
        except httpx.TimeoutException as exc:
            raise insight_timeout(
                base_url=client.base_url,
                path=path,
                timeout_seconds=client._timeout if timeout is None else timeout,
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise insight_upstream_error(
                base_url=client.base_url,
                path=path,
                status_code=exc.response.status_code,
                response_text=exc.response.text,
            ) from exc
        except ValueError as exc:
            raise insight_bad_response(
                base_url=client.base_url, path=path, reason=str(exc)
            ) from exc

    async def _insight_delete_json(self, path: str) -> Any:
        client = self._services.get_insight_http_client()
        try:
            return await client.delete(path)
        except httpx.ConnectError as exc:
            raise insight_unavailable(
                base_url=client.base_url, path=path, reason=str(exc)
            ) from exc
        except httpx.TimeoutException as exc:
            raise insight_timeout(
                base_url=client.base_url,
                path=path,
                timeout_seconds=client._timeout,
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise insight_upstream_error(
                base_url=client.base_url,
                path=path,
                status_code=exc.response.status_code,
                response_text=exc.response.text,
            ) from exc
        except ValueError as exc:
            raise insight_bad_response(
                base_url=client.base_url, path=path, reason=str(exc)
            ) from exc

    @staticmethod
    def _feature_flags(enabled: bool) -> dict[str, bool]:
        return {
            "sessions": enabled,
            "timeseries": enabled,
            "interruptions": enabled,
            "health": enabled,
        }

    @staticmethod
    def _raw_params(
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        buckets: int | None = None,
        session_ids: list[str] | None = None,
    ) -> dict[str, Any] | list[tuple[str, Any]]:
        params: dict[str, Any] = {}
        if start_ns is not None:
            params["start_ns"] = start_ns
        if end_ns is not None:
            params["end_ns"] = end_ns
        if buckets is not None:
            params["buckets"] = buckets
        if session_ids:
            if len(session_ids) == 1:
                params["session_id"] = session_ids[0]
            else:
                multi_params: list[tuple[str, Any]] = list(params.items())
                multi_params.extend(
                    ("session_ids[]", session_id) for session_id in session_ids
                )
                return multi_params
        return params

    def _require_runtime_session(self, witty_session_id: str) -> SessionRecord:
        session = self._repository.get_session(witty_session_id)
        if (
            session is None
            or session.runtime_type != _RUNTIME_TYPE
            or not session.runtime_session_id
        ):
            raise insight_session_mapping_not_found(
                session_id=witty_session_id,
                runtime_type=None if session is None else session.runtime_type,
                runtime_session_id=None
                if session is None
                else session.runtime_session_id,
            )
        return session

    def _remap_interruption_records(self, records: list[Any]) -> list[dict[str, Any]]:
        runtime_session_ids = [
            item.get("session_id")
            for item in records
            if isinstance(item, dict) and isinstance(item.get("session_id"), str)
        ]
        sessions_by_runtime_id = {
            session.runtime_session_id: session
            for session in self._repository.list_sessions_by_runtime_session_ids(
                _RUNTIME_TYPE,
                runtime_session_ids,
            )
            if session.runtime_session_id is not None
        }

        remapped: list[dict[str, Any]] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            payload = dict(item)
            runtime_session_id = payload.get("session_id")
            if isinstance(runtime_session_id, str):
                session = sessions_by_runtime_id.get(runtime_session_id)
                if session is not None:
                    payload["session_id"] = session.id
                    payload["runtime_session_id"] = runtime_session_id
            remapped.append(payload)
        return remapped

    def _list_managed_runtime_session_ids(
        self,
        *,
        witty_agent_id: str | None = None,
    ) -> list[str]:
        if witty_agent_id:
            return self._repository.list_runtime_session_ids_by_agent_id(
                witty_agent_id,
                runtime_type=_RUNTIME_TYPE,
            )

        runtime_session_ids: list[str] = []
        for agent in self._repository.list_agents():
            runtime_session_ids.extend(
                self._repository.list_runtime_session_ids_by_agent_id(
                    agent.id,
                    runtime_type=_RUNTIME_TYPE,
                )
            )
        return self._dedupe_preserve_order(runtime_session_ids)

    @staticmethod
    def _normalize_severity_counts(raw: Any) -> dict[str, int]:
        data = raw if isinstance(raw, dict) else {}
        return {
            severity: int(data.get(severity, 0) or 0)
            for severity in _INTERRUPTION_SEVERITIES
        }

    @staticmethod
    def _empty_interruption_count() -> dict[str, Any]:
        return {
            "total": 0,
            "by_severity": {severity: 0 for severity in _INTERRUPTION_SEVERITIES},
        }

    @staticmethod
    def _dedupe_preserve_order(values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return ordered

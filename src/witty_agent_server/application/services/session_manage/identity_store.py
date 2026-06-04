from dataclasses import dataclass

from witty_agent_server.runtimes.runtime_base import RuntimeType


@dataclass(frozen=True)
class RuntimeSessionIdentity:
    runtime_type: RuntimeType
    runtime_session_key: str
    runtime_session_id: str | None = None


SessionScopeKey = tuple[str, str]


class SessionIdentityStore:
    def __init__(self) -> None:
        self._identities: dict[SessionScopeKey, RuntimeSessionIdentity] = {}

    def bind(
        self,
        *,
        agent_id: str,
        session_id: str,
        runtime_type: RuntimeType,
        runtime_session_key: str,
        runtime_session_id: str | None = None,
    ) -> RuntimeSessionIdentity:
        identity = RuntimeSessionIdentity(
            runtime_type=runtime_type,
            runtime_session_key=runtime_session_key,
            runtime_session_id=runtime_session_id,
        )
        self._identities[(agent_id, session_id)] = identity
        return identity

    def resolve(self, *, agent_id: str, session_id: str) -> RuntimeSessionIdentity | None:
        return self._identities.get((agent_id, session_id))

    def refresh_runtime_session(
        self,
        *,
        runtime_session_key: str,
        runtime_session_id: str,
    ) -> bool:
        changed = False
        for scope_key, identity in self._identities.items():
            if identity.runtime_session_key != runtime_session_key:
                continue

            if identity.runtime_session_id == runtime_session_id:
                return False

            self._identities[scope_key] = RuntimeSessionIdentity(
                runtime_type=identity.runtime_type,
                runtime_session_key=runtime_session_key,
                runtime_session_id=runtime_session_id,
            )
            changed = True
        return changed

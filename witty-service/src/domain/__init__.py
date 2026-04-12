"""Domain primitives for witty service."""

from src.domain.enums import AgentStatus, can_transition
from src.domain.errors import DomainError
from src.domain.models import ErrorPayload

__all__ = [
    "AgentStatus",
    "DomainError",
    "ErrorPayload",
    "can_transition",
]

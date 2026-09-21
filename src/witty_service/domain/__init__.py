"""Domain primitives for witty service."""

from witty_service.domain.enums import AgentStatus, can_transition
from witty_service.domain.errors import (
    AgentConfigUpdateForbiddenError,
    AgentContextMismatchError,
    AgentDefaultNotConfiguredError,
    AgentIdNotConfiguredError,
    AgentNotFoundError,
    AgentServiceError,
    DomainError,
    FieldValidationError,
    InvalidAgentConfigError,
    InvalidAgentTransitionError,
    InvalidSessionMetadataError,
    OpenClawAgentNotFoundError,
    SessionNotFoundError,
)
from witty_service.domain.models import ErrorPayload

__all__ = [
    "AgentConfigUpdateForbiddenError",
    "AgentContextMismatchError",
    "AgentDefaultNotConfiguredError",
    "AgentIdNotConfiguredError",
    "AgentNotFoundError",
    "AgentServiceError",
    "AgentStatus",
    "DomainError",
    "ErrorPayload",
    "FieldValidationError",
    "InvalidAgentConfigError",
    "InvalidAgentTransitionError",
    "InvalidSessionMetadataError",
    "OpenClawAgentNotFoundError",
    "SessionNotFoundError",
    "can_transition",
]

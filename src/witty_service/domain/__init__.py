"""Domain primitives for witty service."""

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
    "DomainError",
    "ErrorPayload",
    "FieldValidationError",
    "InvalidAgentConfigError",
    "InvalidAgentTransitionError",
    "InvalidSessionMetadataError",
    "SessionNotFoundError",
]

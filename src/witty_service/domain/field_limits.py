"""字段取值约束（长度上限的唯一事实来源）：agent 名称/描述、会话标题。

上限与 DB 列宽对齐：agents.name 与 sessions.title 都是 String(255)，超长值会在
commit 时变成 500。API 层（pydantic schema）与 application 层（AgentManager /
SessionManager）共用这里的常量与校验函数：

- API 层负责把明显的坏请求挡在 400/422，给出字段级报错；
- application 层是最终收口——模板实例化、agenthub、渠道等入口不经过 /agents 的
  schema，直接构造 AgentCreateRequest，必须有同一条底线。

任何一处改了上限，另一处自动跟随，避免"schema 放行、DB 报错"。
"""

from __future__ import annotations

from witty_service.domain.errors import (
    InvalidAgentConfigError,
    InvalidSessionMetadataError,
)

#: agents.name 列宽（String(255)）
AGENT_NAME_MAX_LENGTH = 255
#: 产品口径的描述长度上限（DB 为 Text，无硬约束）
AGENT_DESCRIPTION_MAX_LENGTH = 2000
#: sessions.title 列宽（String(255)）
SESSION_TITLE_MAX_LENGTH = 255


def validate_agent_name(name: str) -> None:
    """校验 agent 名称：非空且不超长。"""
    if not name:
        raise InvalidAgentConfigError(field="name", reason="must not be empty")
    if len(name) > AGENT_NAME_MAX_LENGTH:
        raise InvalidAgentConfigError(
            field="name",
            reason="too long",
            max_length=AGENT_NAME_MAX_LENGTH,
        )


def validate_agent_description(description: str) -> None:
    """校验 agent 描述：允许为空，只限长度。"""
    if len(description) > AGENT_DESCRIPTION_MAX_LENGTH:
        raise InvalidAgentConfigError(
            field="description",
            reason="too long",
            max_length=AGENT_DESCRIPTION_MAX_LENGTH,
        )


def validate_session_title(title: str) -> None:
    """校验会话标题：非空且不超长（空标题由自动生成逻辑保证不会被写出）。"""
    if not title:
        raise InvalidSessionMetadataError(field="title", reason="must not be empty")
    if len(title) > SESSION_TITLE_MAX_LENGTH:
        raise InvalidSessionMetadataError(
            field="title",
            reason="too long",
            max_length=SESSION_TITLE_MAX_LENGTH,
        )

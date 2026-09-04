from __future__ import annotations

from witty_agent_server.application.services.session.base import SessionServiceBase


class DshSessionService(SessionServiceBase):
    """dsh session 服务。

    直接继承 SessionServiceBase 复用通用 create/delete/abort/list 流程；
    runtime 差异由 DshRuntime / DshClient 承载（dsh 无会话枚举 API，
    列表由 base 读内存仓库）。
    """

from __future__ import annotations

from typing import Any


class AgentSkillServiceError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class RuntimeSkillsNotSupportedError(AgentSkillServiceError):
    def __init__(self, *, runtime_type: str) -> None:
        super().__init__(
            code="RUNTIME_SKILLS_NOT_SUPPORTED",
            message="runtime skills query is not supported",
            status_code=501,
            details={"runtime_type": runtime_type},
        )


class OpenClawSkillsQueryError(AgentSkillServiceError):
    def __init__(
        self,
        *,
        runtime_type: str,
        code: str,
        message: str,
    ) -> None:
        super().__init__(
            code="OPENCLAW_SKILLS_QUERY_FAILED",
            message="openclaw skills query failed",
            status_code=502,
            details={
                "runtime_type": runtime_type,
                "gateway_error_code": code,
                "gateway_error_message": message,
            },
        )


class OpenClawSkillsInstallError(AgentSkillServiceError):
    def __init__(
        self,
        *,
        runtime_type: str,
        skill_name: str,
        reason: str,
    ) -> None:
        super().__init__(
            code="OPENCLAW_SKILLS_INSTALL_FAILED",
            message="openclaw skills install failed",
            status_code=500,
            details={
                "runtime_type": runtime_type,
                "skill_name": skill_name,
                "reason": reason,
            },
        )


class OpenClawSkillsUninstallError(AgentSkillServiceError):
    def __init__(
        self,
        *,
        runtime_type: str,
        skill_name: str,
        reason: str,
    ) -> None:
        super().__init__(
            code="OPENCLAW_SKILLS_UNINSTALL_FAILED",
            message="openclaw skills uninstall failed",
            status_code=500,
            details={
                "runtime_type": runtime_type,
                "skill_name": skill_name,
                "reason": reason,
            },
        )


class OpenClawSkillNotRemovableError(AgentSkillServiceError):
    def __init__(
        self,
        *,
        runtime_type: str,
        skill_name: str,
        reason: str,
    ) -> None:
        super().__init__(
            code="OPENCLAW_SKILL_NOT_REMOVABLE",
            message="openclaw skill cannot be uninstalled",
            status_code=400,
            details={
                "runtime_type": runtime_type,
                "skill_name": skill_name,
                "reason": reason,
            },
        )


class OpenCodeSkillsQueryError(AgentSkillServiceError):
    def __init__(
        self,
        *,
        runtime_type: str,
        code: str,
        message: str,
    ) -> None:
        super().__init__(
            code="OPENCODE_SKILLS_QUERY_FAILED",
            message="opencode skills query failed",
            status_code=502,
            details={
                "runtime_type": runtime_type,
                "gateway_error_code": code,
                "gateway_error_message": message,
            },
        )


class OpenCodeSkillsInstallError(AgentSkillServiceError):
    def __init__(
        self,
        *,
        runtime_type: str,
        skill_name: str,
        reason: str,
    ) -> None:
        super().__init__(
            code="OPENCODE_SKILLS_INSTALL_FAILED",
            message="opencode skills install failed",
            status_code=500,
            details={
                "runtime_type": runtime_type,
                "skill_name": skill_name,
                "reason": reason,
            },
        )


class OpenCodeSkillsUninstallError(AgentSkillServiceError):
    def __init__(
        self,
        *,
        runtime_type: str,
        skill_name: str,
        reason: str,
    ) -> None:
        super().__init__(
            code="OPENCODE_SKILLS_UNINSTALL_FAILED",
            message="opencode skills uninstall failed",
            status_code=500,
            details={
                "runtime_type": runtime_type,
                "skill_name": skill_name,
                "reason": reason,
            },
        )


# WittyHub 错误码单一事实来源：错误类属性、WITTYHUB_ERROR_BY_CODE 与
# wittyhub_errors 分类器均引用这些常量。
WITTYHUB_SKILL_NOT_FOUND = "WITTYHUB_SKILL_NOT_FOUND"
WITTYHUB_REPO_NOT_INDEXED = "WITTYHUB_REPO_NOT_INDEXED"
WITTYHUB_HUB_UNREACHABLE = "WITTYHUB_HUB_UNREACHABLE"
WITTYHUB_HUB_HTTP_ERROR = "WITTYHUB_HUB_HTTP_ERROR"
WITTYHUB_BAD_SOURCE = "WITTYHUB_BAD_SOURCE"


class WittyHubSkillServiceError(AgentSkillServiceError):
    """wittyhub 技能操作失败基类。

    ``reason`` 为可读文案（上层 witty_service 会将其透传为最终
    details.error），``raw_output`` 保存清洗后的 CLI 原始输出供排障。
    子类按语义覆写 ``status_code``（如 not found → 404、bad source → 400）。
    """

    code = "WITTYHUB_SKILL_INSTALL_FAILED"
    default_message = "wittyhub skill install failed"
    status_code = 500

    def __init__(
        self,
        *,
        runtime_type: str,
        skill_name: str,
        reason: str,
        skill_source: str | None = None,
        raw_output: str | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "runtime_type": runtime_type,
            "skill_name": skill_name,
            "reason": reason,
        }
        if skill_source is not None:
            details["skill_source"] = skill_source
        if raw_output:
            details["raw_output"] = raw_output
        super().__init__(
            code=self.code,
            message=self.default_message,
            status_code=self.status_code,
            details=details,
        )


class WittyHubSkillNotFoundError(WittyHubSkillServiceError):
    code = WITTYHUB_SKILL_NOT_FOUND
    default_message = "wittyhub skill not found"
    status_code = 404


class WittyHubRepoNotIndexedError(WittyHubSkillServiceError):
    code = WITTYHUB_REPO_NOT_INDEXED
    default_message = "wittyhub source repository is not indexed"
    status_code = 404


class WittyHubHubUnreachableError(WittyHubSkillServiceError):
    code = WITTYHUB_HUB_UNREACHABLE
    default_message = "wittyhub skill hub service is unreachable"
    status_code = 502


class WittyHubHubHttpError(WittyHubSkillServiceError):
    code = WITTYHUB_HUB_HTTP_ERROR
    default_message = "wittyhub skill hub service returned an error"
    status_code = 502


class WittyHubBadSourceError(WittyHubSkillServiceError):
    code = WITTYHUB_BAD_SOURCE
    default_message = "wittyhub skill source is invalid"
    status_code = 400


# 错误码 → 错误类映射，供 _run_wittyhub_command 按分类结果抛出
WITTYHUB_ERROR_BY_CODE: dict[str, type[WittyHubSkillServiceError]] = {
    WITTYHUB_SKILL_NOT_FOUND: WittyHubSkillNotFoundError,
    WITTYHUB_REPO_NOT_INDEXED: WittyHubRepoNotIndexedError,
    WITTYHUB_HUB_UNREACHABLE: WittyHubHubUnreachableError,
    WITTYHUB_HUB_HTTP_ERROR: WittyHubHubHttpError,
    WITTYHUB_BAD_SOURCE: WittyHubBadSourceError,
}

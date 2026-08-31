from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DEFAULT_COMMIT_MESSAGE_TEMPLATE = """{{subject}}

{{body_prefix}}

commit {{commit_id}} {{source}}
{{body_separator}}

{{body}}

{{trailers}}"""

COMMIT_MESSAGE_TEMPLATE_DESCRIPTION = (
    "Commit message 模板；{{subject}} 必填，可选变量包括 {{commit_id}}、{{source}}、"
    "{{body_prefix}}、{{body_separator}}、{{body}}、{{trailers}}、{{reference}}、"
    "{{upstream_commit_id}} 和 {{openeuler_commit_id}}。变量之外的文字会原样保留。"
)


class TargetConfigLayoutOpts(BaseModel):
    model_config = {"extra": "forbid"}
    default_level: Literal["L0-MANDATORY", "L1-RECOMMEND", "L2-OPTIONAL"] = Field(
        default_factory=lambda: "L1-RECOMMEND"
    )


class BackportConfigPayload(BaseModel):
    project_url: str = ""
    backport_model_id: str = ""
    project_dir: str = ""
    source_branch: str = ""
    target_path: str = ""
    target_release: str = ""
    patch_dataset_dir: str = ""
    signer_name: str = ""
    signer_email: str = ""
    commit_message_template: str = Field(
        default=DEFAULT_COMMIT_MESSAGE_TEMPLATE,
        description=COMMIT_MESSAGE_TEMPLATE_DESCRIPTION,
    )
    commit_message_source: str = "upstream"
    linux_repo_path: str = "~/Image/linux"
    commit_sort: str = "describe"
    current_excel_path: str = ""
    current_report_path: str = ""
    current_filtered_report_path: str = ""
    target_config_layout: Literal["none", "anolis"] = "none"
    target_config_layout_opts: TargetConfigLayoutOpts = Field(
        default_factory=TargetConfigLayoutOpts
    )
    source_repo_input: str = ""
    target_repo_input: str = ""
    source_repo_state: dict[str, Any] | None = None
    target_repo_state: dict[str, Any] | None = None
    enable_conflict_summary: bool = False
    enable_prerequisite_scan: bool = False
    cvekit_options: dict[str, Any] = Field(default_factory=dict)


class BackportConfigUpdateResponse(BaseModel):
    ok: bool
    config_path: str = ""


class BackportRuntimeStatusResponse(BaseModel):
    ok: bool
    model_configured: bool = False
    model_name: str = ""
    model_provider: str = ""
    api_key_available: bool = False
    cvekit_available: bool = False
    cvekit_path: str = ""
    errors: list[str] = Field(default_factory=list)


class BackportRepositoryPrepareRequest(BaseModel):
    role: str
    input: str
    preferred_branch: str = ""


class BackportRepositoryRefreshRequest(BaseModel):
    role: str
    local_path: str
    source_url: str = ""
    selected_branch: str = ""


class BackportRepositoryPrepareResponse(BaseModel):
    task_id: str
    status: str
    role: str
    input: str
    progress: int = 0
    steps: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""


class BackportRunRequest(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class BackportAsyncRunResponse(BaseModel):
    run_id: str
    action: str
    status: str
    result: dict[str, Any] | None = None
    error: str = ""
    progress: dict[str, Any] | None = None
    execution_summary: dict[str, Any] | None = None
    pause_requested: bool = False
    paused_at: float | None = None


class BackportToolSnapshotResponse(BaseModel):
    tool_name: str
    arguments_text: str
    response_text: str
    is_error: bool


class BackportRunResponse(BaseModel):
    agentId: str
    agentName: str
    sessionId: str
    assistantText: str
    parsedResult: dict[str, Any] | None = None
    toolSnapshots: list[BackportToolSnapshotResponse] = Field(default_factory=list)

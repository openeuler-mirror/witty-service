from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DEFAULT_COMMIT_MESSAGE_TEMPLATE = """{{subject}}

commit {{commit_id}} {{source}}

{{body}}

{{trailers}}"""


class TargetConfigLayoutOpts(BaseModel):
    model_config = {"extra": "forbid"}
    default_level: Literal["L0-MANDATORY", "L1-RECOMMEND", "L2-OPTIONAL"] = Field(
        default_factory=lambda: "L1-RECOMMEND"
    )


class BackportConfigPayload(BaseModel):
    project_url: str = ""
    project_dir: str = ""
    source_branch: str = ""
    target_path: str = ""
    target_release: str = ""
    patch_dataset_dir: str = ""
    signer_name: str = ""
    signer_email: str = ""
    commit_message_template: str = DEFAULT_COMMIT_MESSAGE_TEMPLATE
    commit_message_source: str = "auto"
    linux_repo_path: str = "~/Image/linux"
    commit_sort: str = "describe"
    current_excel_path: str = ""
    current_report_path: str = ""
    current_filtered_report_path: str = ""
    target_config_layout: Literal["none", "anolis"] = "none"
    target_config_layout_opts: TargetConfigLayoutOpts = Field(
        default_factory=TargetConfigLayoutOpts
    )


class BackportConfigUpdateResponse(BaseModel):
    ok: bool
    config_path: str = ""


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

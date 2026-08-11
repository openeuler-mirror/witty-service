from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from witty_service.api.schemas import UtcDatetime


class CreateScheduledTaskRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    schedule_type: Literal["cron", "interval"]
    cron_expr: str | None = Field(default=None, max_length=255)
    interval_seconds: int | None = Field(default=None, gt=0)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    content: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    workspace_folder: str | None = Field(default=None, max_length=512)
    enabled: bool = True

    @model_validator(mode="after")
    def _validate_schedule_fields(self) -> CreateScheduledTaskRequest:
        if self.schedule_type == "cron" and not self.cron_expr:
            raise ValueError("cron_expr is required when schedule_type is 'cron'")
        if self.schedule_type == "interval" and self.interval_seconds is None:
            raise ValueError(
                "interval_seconds is required when schedule_type is 'interval'"
            )
        return self


class UpdateScheduledTaskRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    schedule_type: Literal["cron", "interval"] | None = None
    cron_expr: str | None = Field(default=None, max_length=255)
    interval_seconds: int | None = Field(default=None, gt=0)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    content: str | None = Field(default=None, min_length=1)
    workspace_folder: str | None = Field(default=None, max_length=512)


class RunScheduledTaskRequest(BaseModel):
    """手动触发运行时可选请求体：复用前端已建会话，缺省由后端新建。"""

    session_id: str | None = Field(default=None, min_length=1)


class ScheduledTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    schedule_type: str
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    content: str
    agent_id: str
    workspace_folder: str | None
    enabled: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime
    # 仅当列表接口传入 include_runs=N 时填充；否则为空列表，保证向后兼容。
    recent_runs: list[ScheduledTaskRunResponse] = Field(default_factory=list)


class ScheduledTaskRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    session_id: str | None
    status: str
    error: str | None
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None
    created_at: UtcDatetime


class ScheduledTaskTemplateResponse(BaseModel):
    key: str
    name: str
    description: str
    schedule_type: str
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    content: str
    workspace_folder: str | None

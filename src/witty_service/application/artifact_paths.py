"""``artifact.*`` 事件路径归一化（witty-service 边界）。

Agent 端 ``write`` 工具通常返回工作区内**绝对路径**，而持久化与前端文件
端点都要求工作区相对路径。此处是**唯一包含校验权威**：以 ``agent.workspace_path``
为根，对**绝对与相对路径一律**做 ``resolve() + relative_to`` 校验（见
``resolve_within_workspace``，与文件端点共用同一套规则）：

- 工作区内路径（绝对/相对）→ 归一化为工作区相对路径（同步改写 ``id``）；
- 工作区外路径（含相对路径 ``..`` 逃逸）→ 返回 ``None``（调用方丢弃事件，
  防止工作区外文件内容通过内联 ``content`` 进入事件流 / 落库）；
- 敏感文件（凭据/密钥类）即使内联了 ``content``，也会在边界被剥离。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from witty_agent_server.runtimes.artifact_detector import is_sensitive_file

ARTIFACT_EVENT_TYPES = frozenset(
    {"artifact.started", "artifact.delta", "artifact.completed"}
)


def resolve_within_workspace(workspace_path: str, raw_path: str) -> Path | None:
    """把 ``raw_path`` 解析为工作区内的**绝对路径**，越界则返回 ``None``。

    这是本模块的单一包含校验权威，事件归一化与 ``get_workspace_file`` 复用同一套规则：
    - 绝对路径：必须位于 ``workspace_path`` 内；
    - 相对路径：相对 ``workspace_path`` 拼接后解析；
    - ``..`` 逃逸、符号链接指向工作区外、解析失败 → ``None``。
    """
    if not isinstance(raw_path, str):
        return None
    try:
        workspace_base = Path(workspace_path).resolve()
        resolved = (workspace_base / Path(raw_path)).resolve()
        resolved.relative_to(workspace_base)
    except (ValueError, OSError):
        return None
    return resolved


def normalize_artifact_event(
    event: dict[str, Any], *, workspace_path: str
) -> dict[str, Any] | None:
    """归一化 ``artifact.*`` 事件里的路径为工作区相对路径；非法/越界返回 ``None``（丢弃）。

    - 非 ``artifact.*`` 事件原样透传；
    - ``artifact.*`` 事件：``payload`` 非 dict、``relative_path`` 缺失/为空/非字符串
      （无法做包含校验）→ 丢弃；
    - 对**绝对与相对路径一律做包含校验**：不再信任远端已拒绝 ``..``，任何解析后落在
      工作区外的路径（含相对路径 ``..`` 逃逸、绝对路径偏离）都被丢弃，防止工作区外
      文件内容通过内联 ``content`` 进入事件流 / 落库；
    - 命中敏感文件 denylist 的路径：即使事件内联了 ``content`` 也一并剥离（边界兜底，
      不依赖 agent 侧检测器）。
    """
    if event.get("type") not in ARTIFACT_EVENT_TYPES:
        return event
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    raw_path = payload.get("relative_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    resolved = resolve_within_workspace(workspace_path, raw_path)
    if resolved is None:
        return None
    workspace_base = Path(workspace_path).resolve()
    relative = resolved.relative_to(workspace_base)
    normalized = relative.as_posix()
    if normalized in ("", "."):
        return None
    new_payload: dict[str, Any] = {
        **payload,
        "id": normalized,
        "relative_path": normalized,
    }
    if is_sensitive_file(normalized):
        new_payload.pop("content", None)
    return {**event, "payload": new_payload}


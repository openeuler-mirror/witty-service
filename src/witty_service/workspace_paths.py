"""Agent witty workspace 路径的唯一事实来源。

本模块承载三个单一来源，供 ``witty_service`` 与 ``witty_agent_server`` 共同引用：

- ``workspace_root``: 全局唯一的 workspace 根目录（``resolve()`` 归一化）。
- ``validate_agent_id``: 唯一的 agent_id 合法性校验（拒绝路径穿越/非法目录名）。
- ``agent_workspace_path``: 唯一的 agent workspace 路径推导
  ``<workspace-root>/agent-workspaces/<agent_id>/workspace``。

之所以放在 ``witty_service`` 而不是 ``witty_agent_server``，是因为依赖方向为
``witty_agent_server → witty_service``；放在更底层才能让两侧都导入，从而真正单点。
"""

from __future__ import annotations

from pathlib import Path

from witty_service.config import get_settings


def workspace_root(*, resolve: bool = True) -> Path:
    """返回全局唯一的工作空间根目录。

    ``resolve=True`` 时用 ``resolve()`` 归一化（展开符号链接、转绝对路径），
    保证各处（``WorkspaceStore``、``agent_workspace_path``）对根目录都拿到同一个值。
    """
    path = get_settings().workspace.root_path()
    return path.resolve() if resolve else path


def validate_agent_id(agent_id: str) -> None:
    """校验 agent_id，拒绝路径穿越与非法目录名。

    ``agent_id`` 必须是一个合法的单段目录名：非空、不能是 ``.``/``..``、
    不得包含 ``/`` 或 ``\\``、不得为绝对路径。该约束保证 ``agent_workspace_path``
    推导出的路径永远落在 workspace 根之下。
    """
    if not agent_id:
        raise ValueError("agent_id must not be empty")
    if agent_id in {".", ".."}:
        raise ValueError(f"Invalid agent_id: {agent_id!r}")
    if any(separator in agent_id for separator in ("/", "\\")):
        raise ValueError(f"Invalid agent_id: {agent_id!r}")
    agent_path = Path(agent_id)
    if (
        agent_path.is_absolute()
        or agent_path.parts != (agent_id,)
        or agent_path.name != agent_id
    ):
        raise ValueError(f"Invalid agent_id: {agent_id!r}")


def agent_root_path(agent_id: str, *, root: Path | None = None) -> Path:
    """返回 agent 的 witty 根目录 ``<root>/agent-workspaces/<agent_id>``。

    与 ``agent_workspace_path`` 同为路径事实源，但**不解析符号链接**：删除场景
    需要先按原始路径判定目录归属，避免 ``resolve()`` 跟随软链后删到根目录之外。
    """
    validate_agent_id(agent_id)
    base = root if root is not None else workspace_root(resolve=False)
    return base / "agent-workspaces" / agent_id


def agent_workspace_path(agent_id: str, *, root: Path | None = None) -> Path:
    """返回 agent 的 witty workspace 路径。

    单一事实来源：``<workspace-root>/agent-workspaces/<agent_id>/workspace``。
    先经 ``validate_agent_id`` 校验，再用 ``resolve()`` 归一化，确保与
    ``WorkspaceStore`` 创建、以及持久化的 ``agent.workspace_path`` 保持一致。
    未显式传入 ``root`` 时使用 ``workspace_root()``。
    """
    return (agent_root_path(agent_id, root=root) / "workspace").resolve()

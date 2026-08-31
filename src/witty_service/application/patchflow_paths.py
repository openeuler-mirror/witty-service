from __future__ import annotations

import os
from pathlib import Path

PATCHFLOW_STATE_ROOT_ENV = "PATCHFLOW_STATE_ROOT"


def resolve_patchflow_state_root(
    configured_root: str | Path | None = None,
) -> Path:
    """Resolve the shared Patchflow locks/logs root without requiring writable HOME."""
    value = (
        configured_root
        if configured_root is not None
        else os.environ.get(PATCHFLOW_STATE_ROOT_ENV)
    )
    if value:
        return Path(value).expanduser().resolve()
    return (Path.home() / ".patchflow").resolve()

import logging
import tarfile
from pathlib import Path
from pkgutil import get_data

from witty_service.config import get_settings

logger = logging.getLogger(__name__)


def init_workspace(workspace_root: str | None = None) -> Path:
    if workspace_root is None:
        workspace_root = get_settings().workspace.root

    workspace_path = Path(workspace_root).expanduser()

    if workspace_path.exists():
        logger.info(f"Workspace already exists at {workspace_path}")
        return workspace_path

    logger.info(f"Initializing workspace at {workspace_path}")

    try:
        workspace_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        fallback_path = (
            Path.home() / ".witty"
            if str(workspace_path) != str(Path.home() / ".witty")
            else Path("/tmp/witty-workspace")  # nosec B108
        )
        logger.warning(
            f"Failed to create workspace at {workspace_path}: {e}, trying fallback: {fallback_path}"
        )
        workspace_path = fallback_path
        workspace_path.mkdir(parents=True, exist_ok=True)

    tar_data = get_data("witty_service", "data/agent-config.tar.gz")
    if tar_data is None:
        logger.error("Failed to find agent-config.tar.gz in package data")
        raise FileNotFoundError("agent-config.tar.gz not found in package")

    tar_path = workspace_path / "agent-config.tar.gz"
    tar_path.write_bytes(tar_data)

    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            for member in tar.getmembers():
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"unsafe tar member: {member.name}")

            tar.extractall(path=workspace_path)  # nosec B202
        logger.info(f"Successfully extracted agent-config to {workspace_path}")
    except Exception as e:
        logger.error(f"Failed to extract agent-config: {e}")
        raise
    finally:
        tar_path.unlink(missing_ok=True)

    return workspace_path

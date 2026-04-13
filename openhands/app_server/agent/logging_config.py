"""File logging for the agent middleware package under ~/.agentd."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

_AGENT_LOGGER_NAME = "openhands.app_server.agent"
_DEFAULT_DIR = Path.home() / ".agentd"
_DEFAULT_FILE = "agentd.log"
_MAX_BYTES = int(os.getenv("AGENTD_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
_BACKUP_COUNT = int(os.getenv("AGENTD_LOG_BACKUP_COUNT", "5"))

_lock = threading.Lock()
_configured = False


def _parse_level(name: str | None) -> int:
    if not name:
        return logging.INFO
    return getattr(logging, name.upper(), logging.INFO)


def get_agentd_log_dir() -> Path:
    """Resolved log directory (default ~/.agentd).

    Under pytest without ``AGENTD_LOG_DIR``, uses a temp dir so ``~/.agentd`` is not
    filled by test runs.
    """
    raw = os.getenv("AGENTD_LOG_DIR")
    if raw:
        return Path(raw).expanduser()
    if os.getenv("PYTEST_CURRENT_TEST"):
        return Path(tempfile.gettempdir()) / "agentd-pytest-logs"
    # Use project directory instead of home directory to avoid permission issues
    return Path(".") / ".agentd"


def configure_agentd_logging(
    log_dir: Path | str | None = None,
    log_file: str | None = None,
    level: int | str | None = None,
    force: bool = False,
) -> Path:
    """Attach a rotating file handler to the package logger namespace.

    - Directory: ``AGENTD_LOG_DIR`` or ``~/.agentd``
    - File: ``AGENTD_LOG_FILE`` basename (default ``agentd.log``) under that directory
    - Level: ``AGENTD_LOG_LEVEL`` (default INFO) or *level* argument

    Safe to call multiple times; no duplicate file handlers unless *force* is True.
    Returns the path to the primary log file.
    """
    global _configured
    with _lock:
        log_root = logging.getLogger(_AGENT_LOGGER_NAME)
        if os.getenv("AGENTD_LOG_DISABLE", "").strip().lower() in ("1", "true", "yes"):
            _configured = True
            return get_agentd_log_dir() / (log_file or os.getenv("AGENTD_LOG_FILE", _DEFAULT_FILE))

        log_path = Path(log_dir).expanduser() if log_dir else get_agentd_log_dir()
        log_path.mkdir(parents=True, exist_ok=True)
        filename = log_file or os.getenv("AGENTD_LOG_FILE", _DEFAULT_FILE)
        file_path = log_path / filename
        file_path_resolved = file_path.resolve()

        if not force and _configured:
            return file_path_resolved

        if force:
            for h in list(log_root.handlers):
                if isinstance(h, RotatingFileHandler):
                    p = getattr(h, "baseFilename", None)
                    if p and Path(p).resolve() == file_path_resolved:
                        log_root.removeHandler(h)
                        h.close()

        lev = level if level is not None else _parse_level(os.getenv("AGENTD_LOG_LEVEL"))
        if isinstance(lev, str):
            lev = _parse_level(lev)
        log_root.setLevel(lev)

        handler = RotatingFileHandler(
            file_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setLevel(lev)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        log_root.addHandler(handler)
        # Children (e.g. ...agent.agent_manager) propagate here and to root if needed.
        log_root.propagate = True

        _configured = True
        return file_path_resolved

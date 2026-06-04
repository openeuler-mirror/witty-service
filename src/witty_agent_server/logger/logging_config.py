from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    """初始化全局日志配置。重复调用时不覆盖已有 handler。"""
    level_name = os.getenv("WITTY_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()
    if root_logger.handlers:
        root_logger.setLevel(level)
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s",
    )

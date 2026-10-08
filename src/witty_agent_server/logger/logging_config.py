from __future__ import annotations

import logging

from witty_service.config import get_settings


def configure_logging() -> None:
    """初始化全局日志配置。重复调用时不覆盖已有 handler。"""
    settings = get_settings()
    level_name = settings.logging.level
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()
    if root_logger.handlers:
        root_logger.setLevel(level)
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # 与 witty-service 侧 logger.py 保持一致
    logging.getLogger("httpx").setLevel(logging.WARNING)

"""
日志模块：统一配置日志输出到控制台和文件。
"""

import os
import logging
from logging.handlers import RotatingFileHandler


def setup_logger(config: dict) -> logging.Logger:
    """
    根据配置初始化全局 logger。

    Args:
        config: 配置字典，包含 logging 段

    Returns:
        配置好的 Logger 实例
    """
    log_cfg = config.get("logging", {})
    level_name = log_cfg.get("level", "INFO").upper()
    log_file = log_cfg.get("file", "data/logs/scraper.log")
    max_bytes = log_cfg.get("max_size_mb", 10) * 1024 * 1024
    backup_count = log_cfg.get("backup_count", 5)

    # 确保日志目录存在
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("xueqiu_scraper")
    logger.setLevel(getattr(logging, level_name, logging.INFO))

    # 避免重复添加 handler（模块被多次 import 时）
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出（轮转）
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    """获取已配置的 logger 实例。如果尚未初始化，返回基础 logger。"""
    return logging.getLogger("xueqiu_scraper")

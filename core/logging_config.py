from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from typing import Any


class FriendlyLogFormatter(logging.Formatter):
    """
    面向终端阅读的日志格式。
    """

    LEVEL_COLORS = {
        logging.DEBUG: "\x1b[2m",
        logging.INFO: "\x1b[36m",
        logging.WARNING: "\x1b[33m",
        logging.ERROR: "\x1b[31m",
        logging.CRITICAL: "\x1b[1;31m",
    }
    RESET = "\x1b[0m"

    def __init__(self, *, use_colors: bool = True) -> None:
        super().__init__()
        self._use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        time_text = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        level_text = f"{record.levelname:<8}"
        logger_name = _compact_logger_name(record.name)
        message = record.getMessage()

        if self._use_colors:
            color = self.LEVEL_COLORS.get(record.levelno, "")
            if color:
                level_text = f"{color}{level_text}{self.RESET}"

        formatted = f"{time_text} | {level_text} | {logger_name:<32} | {message}"
        if record.exc_info:
            formatted = f"{formatted}\n{self.formatException(record.exc_info)}"
        return formatted

    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        base_time = super().formatTime(record, datefmt)
        return f"{base_time}.{int(record.msecs):03d}"


def configure_logging(*, level: str = "INFO", use_colors: bool = True) -> None:
    """
    配置应用日志输出。
    """

    resolved_level = _resolve_log_level(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FriendlyLogFormatter(use_colors=use_colors))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(resolved_level)
    root_logger.addHandler(handler)

    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("pydoll").setLevel(logging.WARNING)


def sanitize_url(url: str) -> str:
    return url


def sanitize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item_value for key, item_value in value.items()}


def mask_email(email_address: str | None) -> str:
    return email_address or ""


def mask_phone(phone_number: str | None) -> str:
    return phone_number or ""


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"


def _compact_logger_name(name: str) -> str:
    if len(name) <= 32:
        return name
    parts = name.split(".")
    if len(parts) <= 1:
        return name[-32:]
    compact_parts = [part[:1] for part in parts[:-1]]
    compact_name = ".".join([*compact_parts, parts[-1]])
    if len(compact_name) <= 32:
        return compact_name
    return compact_name[-32:]


def _resolve_log_level(level: str) -> int:
    normalized_level = level.upper()
    resolved_level = logging.getLevelName(normalized_level)
    if not isinstance(resolved_level, int):
        raise ValueError(f"不支持的日志级别: {level}")
    return resolved_level


for logger_name in ("account", "account_export", "core", "email", "register", "sms"):
    project_logger = logging.getLogger(logger_name)
    if not project_logger.handlers:
        project_logger.addHandler(logging.NullHandler())

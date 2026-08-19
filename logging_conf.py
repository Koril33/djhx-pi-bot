"""结构化日志：python-json-logger，dictConfig 配置。

- 控制台（stdout）输出 JSON 行，便于 systemd journal / 采集
- 文件输出按天滚动
- 通过 logger 的 extra 字段携带丰富上下文（user_id / chat_id / session / 耗时等）
"""
import logging
import logging.config
import os
from datetime import datetime, timezone
from typing import Any

from pythonjsonlogger.json import JsonFormatter


class ContextFormatter(JsonFormatter):
    """JSON 格式化器：统一补充 timestamp / level / logger，并保留 extra 字段。"""

    def add_fields(self, log_record: dict[str, Any], record: logging.LogRecord, message_dict: dict[str, Any]) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        log_record["message"] = record.getMessage()
        # 去掉默认冗余字段，保留 extra
        log_record.pop("asctime", None)


def _configure(level: str, log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {"()": ContextFormatter, "format": "%(message)s"},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                },
                "file": {
                    "class": "logging.handlers.TimedRotatingFileHandler",
                    "formatter": "json",
                    "filename": os.path.join(log_dir, "bot.log"),
                    "when": "midnight",
                    "backupCount": 14,
                    "encoding": "utf-8",
                },
            },
            "root": {
                "level": level,
                "handlers": ["console", "file"],
            },
            "loggers": {
                # 压掉三方库的噪声
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "telegram": {"level": "INFO"},
                "uvicorn": {"level": "INFO"},
                "uvicorn.access": {"level": "INFO"},
            },
        }
    )


def setup_logging(level: str | None = None, log_dir: str | None = None) -> None:
    from config import get_settings

    s = get_settings()
    _configure(level or s.log_level, log_dir or s.log_dir)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

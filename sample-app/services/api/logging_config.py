"""Structured JSON logging configuration for the sample API service.

Reads from env:
    LOG_PATH     File to write logs to (default: /app/logs/app.log).
    LOG_LEVEL    Minimum level: DEBUG | INFO | WARNING | ERROR (default: INFO).
    SERVICE_NAME Label attached to every record (default: api).

Each record is one JSON line:
    {"timestamp": "...", "level": "INFO", "message": "...", "service": "api",
     "logger": "api.routes", "request_id": "..."}

Uses RotatingFileHandler (MAX_BYTES=10 MB, BACKUP_COUNT=3) so logs never
grow unboundedly. Call setup_logging() once at application startup.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

LOG_PATH: str = os.environ.get("LOG_PATH", "/app/logs/app.log")
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()
SERVICE_NAME: str = os.environ.get("SERVICE_NAME", "api")
MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT: int = 3


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single newline-terminated JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f"),
            "level": record.levelname,
            "message": record.getMessage(),
            "service": SERVICE_NAME,
            "logger": record.name,
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_path: Optional[str] = None) -> logging.Logger:
    """Configure application-wide structured logging. Safe to call multiple times."""
    path = Path(log_path or LOG_PATH)
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    fmt = _JsonFormatter()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers[0].setFormatter(fmt)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(path), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        handlers.append(file_handler)
    except OSError as exc:
        print(
            f"[logging_config] ERROR: cannot open log file {path}: {exc}",
            file=sys.stderr,
        )

    logging.basicConfig(level=level, handlers=handlers, force=True)
    logger = logging.getLogger(SERVICE_NAME)
    logger.setLevel(level)
    return logger

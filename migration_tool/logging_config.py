"""Structured logging.

Every migration item emits one line with a stable shape:

    {"event": "migration.item", "run_id": "...", "entity": "user",
     "legacy_id": 37, "operation": "import", "result": "created",
     "duration_ms": 12.4}
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_STD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_") and key not in payload:
                payload[key] = value
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STD_ATTRS and not k.startswith("_")
        }
        tail = " ".join(f"{k}={v}" for k, v in extra.items())
        base = f"{record.levelname:<7} {record.getMessage()}"
        return f"{base}  {tail}".rstrip()


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger("migration_tool")
    root.handlers = [handler]
    root.setLevel(level.upper())
    root.propagate = False


def get_logger(name: str = "migration_tool") -> logging.Logger:
    return logging.getLogger(name)

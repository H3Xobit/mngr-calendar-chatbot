"""Centralised logging with two output modes.

Text mode (default) is human-friendly for local development:
    2026-05-13 09:00:01 | INFO    | services.chat_service | Tool find_free_slots ok

JSON mode (`LOG_FORMAT=json`) is one structured object per line for production
log aggregators (Datadog, Honeycomb, Loki, etc.):
    {"ts": "...", "level": "INFO", "name": "...", "msg": "...", "request_id": "..."}

Both modes write to ``logs/app.log`` (rotated at 2 MB) AND stdout so uvicorn
shows them while developing.

Each FastAPI request gets a UUIDv4 `request_id` injected into a contextvar by
``main.py``'s middleware. The custom log filter pulls that into every record
so a single request's logs are trivially correlatable.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import sys
import uuid as _uuid
from pathlib import Path
from typing import ClassVar

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "app.log"

_TEXT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | rid=%(request_id)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Per-request correlation id, populated by main.py's RequestIDMiddleware.
_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def new_request_id() -> str:
    """Generate a short request id (uuid4 first 8 chars)."""
    return _uuid.uuid4().hex[:8]


def bind_request_id(rid: str) -> contextvars.Token:
    """Bind a request id for the lifetime of the current async task."""
    return _request_id_ctx.set(rid)


def unbind_request_id(token: contextvars.Token) -> None:
    _request_id_ctx.reset(token)


def current_request_id() -> str:
    return _request_id_ctx.get()


class _RequestIDFilter(logging.Filter):
    """Inject the current request id into every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


class _JSONFormatter(logging.Formatter):
    """One JSON object per log line. Stable key order."""

    _BUILTIN_ATTRS: ClassVar[set[str]] = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, _DATEFMT),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        # Include any structured extras attached via logger.info("...", extra={...}).
        for k, v in record.__dict__.items():
            if k in self._BUILTIN_ATTRS or k.startswith("_") or k == "request_id":
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _build_handlers() -> list[logging.Handler]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    use_json = os.getenv("LOG_FORMAT", "text").lower() == "json"
    formatter: logging.Formatter = (
        _JSONFormatter() if use_json else logging.Formatter(_TEXT_FORMAT, datefmt=_DATEFMT)
    )

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_RequestIDFilter())

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(_RequestIDFilter())

    return [file_handler, stream_handler]


def configure_root_logger(level: int = logging.INFO) -> None:
    """Idempotently configure the root logger for the whole app."""
    root = logging.getLogger()
    if getattr(root, "_mngr_configured", False):
        return
    root.setLevel(level)
    for h in _build_handlers():
        root.addHandler(h)
    # Quiet down some noisy third-party libraries.
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    root._mngr_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger; configures root on first use."""
    configure_root_logger(
        level=logging.DEBUG if os.getenv("DEBUG") == "1" else logging.INFO
    )
    return logging.getLogger(name)

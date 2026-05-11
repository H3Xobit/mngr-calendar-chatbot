"""Centralised logging.

All logs (info + errors) are written to ``logs/app.log`` with a timestamp,
and also mirrored to stdout so ``uvicorn`` shows them while developing.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "app.log"

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _build_handlers() -> list[logging.Handler]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

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

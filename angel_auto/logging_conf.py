"""Structured logging: JSON to a rotating file, human-readable to stdout.

Call configure_logging() once at process startup (each entry point in scripts/ does this).
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import structlog

from angel_auto.settings import get_settings


def configure_logging() -> None:
    settings = get_settings()
    log_cfg = settings.app.logging

    log_path = Path(log_cfg.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, log_cfg.level.upper(), logging.INFO)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    stream_handler = logging.StreamHandler()

    logging.basicConfig(level=level, handlers=[file_handler, stream_handler], format="%(message)s")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)

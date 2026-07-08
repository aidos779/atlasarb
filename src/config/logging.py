"""Structured logging setup (structlog). Console in dev, JSON in prod.

Secrets/PII are never logged directly — callers pass structured fields and the
error middleware sanitizes user-facing messages separately (NFR-SEC: error sanitization).
"""
from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json_output:
        renderer: structlog.typing.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def describe_exc(exc: BaseException) -> str:
    """Human-readable one-liner for an exception.

    Many transport exceptions (asyncio.TimeoutError, ConnectionResetError,
    aiohttp.ServerDisconnectedError, bare ConnectionError()) have an empty
    ``str()``, which produced useless ``error=`` fields in the logs. Always
    include the type name so the record is diagnosable even when the message
    is empty.
    """
    message = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message else name

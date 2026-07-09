"""Structured logging setup (structlog). Console in dev, JSON in prod.

Secrets/PII are never logged directly — callers pass structured fields and the
error middleware sanitizes user-facing messages separately (NFR-SEC: error sanitization).
"""
from __future__ import annotations

import logging
import sys
import time

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


class LogThrottle:
    """Aggregate identical log events so a repeating failure emits once per window.

    ``allow(key)`` returns ``(emit, suppressed)``: ``emit`` is True when the caller
    should log now, and ``suppressed`` is how many identical events were swallowed
    since the last emission (include it in the log record so nothing is lost).
    The first occurrence of a key always emits — errors are never delayed, only
    their repetitions are collapsed.
    """

    def __init__(self, interval_sec: float = 60.0) -> None:
        self._interval = interval_sec
        self._last_emit: dict[object, float] = {}
        self._suppressed: dict[object, int] = {}

    def allow(self, key: object, now: float | None = None) -> tuple[bool, int]:
        now = time.monotonic() if now is None else now
        last = self._last_emit.get(key)
        if last is None or now - last >= self._interval:
            suppressed = self._suppressed.pop(key, 0)
            self._last_emit[key] = now
            # Bound the key set: drop stale entries opportunistically.
            if len(self._last_emit) > 512:
                cutoff = now - self._interval * 4
                for k in [k for k, t in self._last_emit.items() if t < cutoff]:
                    self._last_emit.pop(k, None)
                    self._suppressed.pop(k, None)
            return True, suppressed
        self._suppressed[key] = self._suppressed.get(key, 0) + 1
        return False, 0


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

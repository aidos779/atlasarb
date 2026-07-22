"""Structured logging setup (structlog). Console in dev, JSON in prod.

Secrets/PII are never logged directly — callers pass structured fields and the
error middleware sanitizes user-facing messages separately (NFR-SEC: error sanitization).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

import structlog

# Whether DEBUG records are actually emitted. structlog's filtering bound logger makes
# ``log.debug(...)`` a no-op below its level, but Python still evaluates every keyword
# argument at the call site first — and on the hot path those arguments are Decimal
# divisions, rounds and float() conversions whose results are then thrown away. Hot-path
# callers gate on ``debug_enabled()`` so that work is skipped entirely.
#
# Defaults to True: an unconfigured structlog emits every level, so callers that gate on
# this behave exactly as they did before configure_logging() ran (e.g. in tests).
_debug_enabled = True


def debug_enabled() -> bool:
    """True when DEBUG records are actually emitted.

    Only for guarding *expensive* debug-argument construction on the hot path. Cheap
    ``log.debug()`` calls need no guard — the filtering logger already drops them.
    """
    return _debug_enabled


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    global _debug_enabled
    log_level = getattr(logging, level.upper(), logging.INFO)
    _debug_enabled = log_level <= logging.DEBUG

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


_hooks_installed = False


def install_global_exception_hooks(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Route every unhandled exception through structlog instead of raw stderr.

    Three escape hatches existed for a traceback to reach stderr unstructured — and in
    JSON-log deployments an unstructured traceback is unparseable noise that no alert
    rule can match:

      * ``sys.excepthook``          — a crash on the main thread (e.g. out of ``main()``)
      * ``threading.excepthook``   — a crash inside any worker thread
      * the asyncio exception handler — a task that died with nobody awaiting it, and
        "Task exception was never retrieved" at GC time

    All three now emit a single ``[error]``-level structured record carrying the full
    traceback in ``exc_info``. ``KeyboardInterrupt`` is deliberately passed through to
    the default hook so Ctrl-C stays a clean, quiet shutdown rather than an error.

    Idempotent — safe to call from both the composition root and tests.
    """
    global _hooks_installed
    log = get_logger("unhandled")

    def _sys_excepthook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error("unhandled_exception", scope="main_thread",
                  error=describe_exc(exc), exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        log.error("unhandled_exception", scope="thread",
                  thread=getattr(args.thread, "name", None),
                  error=describe_exc(args.exc_value) if args.exc_value else args.exc_type.__name__,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _sys_excepthook
    threading.excepthook = _thread_excepthook

    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    if loop is not None:
        loop.set_exception_handler(_asyncio_exception_handler)
    _hooks_installed = True


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """asyncio loop hook — the one that catches orphaned-task tracebacks.

    A background task that raises with no one awaiting it (every ``create_task`` in the
    scanner/notification pipeline) surfaces here. Previously that printed a bare
    traceback to stderr at GC time with no signal id, no task name, and no severity tag.
    """
    log = get_logger("unhandled")
    exc = context.get("exception")
    task = context.get("task") or context.get("future")
    fields = {
        "scope": "asyncio",
        "message": context.get("message", ""),
        "task": getattr(task, "get_name", lambda: None)() if task is not None else None,
    }
    if isinstance(exc, asyncio.CancelledError):
        # Cooperative shutdown, not a failure — every stop() cancels its task.
        log.debug("task_cancelled", **fields)
        return
    if exc is not None:
        log.error("unhandled_task_exception", error=describe_exc(exc), exc_info=exc,
                  **fields)
    else:
        log.error("asyncio_loop_error", **fields)


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


def redact_url(url: str) -> str:
    """Strip credentials from a provider URL for safe logging.

    RPC provider URLs embed API keys as a path segment (``…/v2/<key>``) or a query param
    (``?apikey=<key>``). Log the scheme + host only, with a ``/…`` marker when a path/query
    was present, so the provider is still identifiable (and the ``network`` is logged as a
    separate field) but no key/token/credential can leak. Non-URL inputs are returned
    unchanged; anything unparseable collapses to ``***``.
    """
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    if not parts.scheme or not parts.netloc:
        return url  # not a full URL (e.g. a bare host) — no embedded secret to strip
    tail = "/…" if (parts.path not in ("", "/") or parts.query) else ""
    return f"{parts.scheme}://{parts.netloc}{tail}"

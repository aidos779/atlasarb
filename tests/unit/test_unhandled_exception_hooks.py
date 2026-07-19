"""Global unhandled-exception hooks (issue #8).

Three escape hatches let a raw traceback reach stderr unstructured — a main-thread
crash, a worker-thread crash, and an orphaned asyncio task. In a JSON-log deployment an
unstructured traceback is unparseable noise no alert rule can match. All three now emit
a single `[error]`-level structured record carrying the traceback.
"""
from __future__ import annotations

import asyncio
import sys
import threading

import pytest
import structlog

from src.config.logging import install_global_exception_hooks


@pytest.fixture
def captured():
    """Capture structlog events without touching the process-wide renderer."""
    logs = structlog.testing.LogCapture()
    structlog.configure(processors=[logs])
    yield logs.entries
    structlog.reset_defaults()


@pytest.fixture(autouse=True)
def _restore_hooks():
    saved = (sys.excepthook, threading.excepthook)
    yield
    sys.excepthook, threading.excepthook = saved


def test_main_thread_exception_is_logged_at_error(captured):
    install_global_exception_hooks(loop=None)
    try:
        raise ValueError("boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    (entry,) = captured
    assert entry["event"] == "unhandled_exception"
    assert entry["log_level"] == "error"          # not a generic warning
    assert entry["scope"] == "main_thread"
    assert "ValueError: boom" in entry["error"]
    assert entry["exc_info"] is not None          # full traceback preserved


def test_keyboard_interrupt_is_not_an_error(captured):
    """Ctrl-C is a clean shutdown, not an incident to page on."""
    install_global_exception_hooks(loop=None)
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        sys.excepthook(*sys.exc_info())
    assert captured == []


def test_thread_exception_is_logged_at_error(captured):
    install_global_exception_hooks(loop=None)

    def _crash():
        raise RuntimeError("thread died")

    thread = threading.Thread(target=_crash, name="worker-7")
    thread.start()
    thread.join()

    (entry,) = captured
    assert entry["event"] == "unhandled_exception"
    assert entry["log_level"] == "error"
    assert entry["scope"] == "thread"
    assert entry["thread"] == "worker-7"


async def test_orphaned_task_exception_is_logged_at_error(captured):
    """The case that mattered: every create_task in the scanner is fire-and-forget."""
    loop = asyncio.get_running_loop()
    install_global_exception_hooks(loop)

    loop.call_exception_handler({
        "message": "Task exception was never retrieved",
        "exception": ZeroDivisionError("division by zero"),
    })

    (entry,) = captured
    assert entry["event"] == "unhandled_task_exception"
    assert entry["log_level"] == "error"
    assert entry["scope"] == "asyncio"
    assert "ZeroDivisionError" in entry["error"]


async def test_cancellation_is_not_an_error(captured):
    """Every stop() cancels its task — cooperative shutdown must stay quiet."""
    loop = asyncio.get_running_loop()
    install_global_exception_hooks(loop)

    loop.call_exception_handler({"message": "cancelled",
                                 "exception": asyncio.CancelledError()})

    assert [e for e in captured if e["log_level"] == "error"] == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])

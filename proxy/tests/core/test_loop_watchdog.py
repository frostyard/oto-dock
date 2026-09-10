"""Event-loop stall watchdog (core/loop_watchdog.py)."""

import asyncio
import logging
import threading

import pytest

from core import loop_watchdog as wd


@pytest.fixture(autouse=True)
def _clean():
    wd.stop()
    wd.reset_stats()
    yield
    wd.stop()
    wd.reset_stats()


def _blocking_call_for_the_test(warning_seen):
    # Keep the loop blocked until the independent watchdog actually samples
    # the stall, rather than racing its 0.5-second polling interval.
    assert warning_seen.wait(timeout=5), "no stall warning logged"


@pytest.mark.asyncio
async def test_stall_is_logged_with_loop_stack_and_counted(caplog):
    caplog.set_level(logging.INFO, logger="claude-proxy.loop-watchdog")
    warning_seen = threading.Event()
    recovery_seen = threading.Event()

    class WatchdogEvents(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if record.levelno == logging.WARNING and "event loop stalled" in message:
                warning_seen.set()
            elif record.levelno == logging.INFO and "recovered after" in message:
                recovery_seen.set()

    handler = WatchdogEvents()
    wd.logger.addHandler(handler)
    try:
        assert wd.start(threshold_s=0.2)
        await asyncio.sleep(0)  # let the newly scheduled heartbeat initialize
        _blocking_call_for_the_test(warning_seen)
        # Waiting in a worker leaves the event loop free to resume heartbeats.
        assert await asyncio.to_thread(recovery_seen.wait, 5), "no recovery logged"
    finally:
        wd.stop()
        wd.logger.removeHandler(handler)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "no stall warning logged"
    assert "event loop stalled" in warnings[0].getMessage()
    assert "_blocking_call_for_the_test" in warnings[0].getMessage()
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("recovered after" in m for m in infos)
    s = wd.stats()
    assert s["stalls"] == 1
    assert s["last_stall_s"] >= s["threshold_s"]
    assert s["max_stall_s"] == s["last_stall_s"]


@pytest.mark.asyncio
async def test_disabled_when_threshold_zero():
    assert wd.start(threshold_s=0) is False
    assert not wd.is_running()
    assert wd.stats()["enabled"] is False


@pytest.mark.asyncio
async def test_stop_before_tick_cancel_logs_nothing(caplog):
    caplog.set_level(logging.INFO, logger="claude-proxy.loop-watchdog")
    assert wd.start(threshold_s=0.2)
    await asyncio.sleep(0.6)
    wd.stop()  # flags the thread first, then cancels the tick task
    await asyncio.sleep(0.8)  # no ticks now — must NOT read as a stall
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not wd.is_running()


@pytest.mark.asyncio
async def test_start_is_idempotent():
    assert wd.start(threshold_s=0.5)
    assert wd.start(threshold_s=0.5) is False
    wd.stop()
    wd.stop()  # idempotent

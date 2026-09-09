"""Event-loop stall watchdog — production monitoring for the one thread that
serves every WebSocket ping, login and dashboard frame.

Why: on 2026-09-03 the internal proxy froze for up to three minutes at a time
while its neighbours on the same VM kept running. The logs held NO line from
the frozen window, so the blocking call had to be reconstructed from health-
probe gaps and image-unpack timestamps a day later. This module makes the
next stall self-describing: when the loop stops ticking for longer than
``threshold_s`` a daemon thread logs a WARNING with the loop thread's stack
(``sys._current_frames``) — the exact frame that is blocking — repeats it
every ``_REPEAT_S`` while the stall lasts, and logs the recovery with the
total duration. Counters are exposed via ``stats()`` (``GET /health``
``loop`` block).

Cost: one 0.5 s timer on the loop + one sleeping daemon thread. Disabled
entirely with a threshold ``<= 0`` (``LOOP_WATCHDOG_THRESHOLD_S``).

Ordering (load-bearing, see startup.py): ``start()`` runs as the LAST boot
step, after the synchronous schema init / preflight / manifest scan, so a
long boot never logs a fake stall; ``stop()`` flags the thread BEFORE the
tick task is cancelled, so shutdown never logs one either.

Known limit: a C-level call that holds the GIL (a huge ``json.loads``) hides
the Python stack until it returns; blocking waits such as psycopg's release
the GIL, so the stall class this was built for is observable.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger("claude-proxy.loop-watchdog")

_TICK_S = 0.5
_REPEAT_S = 10.0
_STACK_FRAMES = 25

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_tick_task: asyncio.Task | None = None
_last_tick: float = 0.0
_loop_thread_ident: int | None = None
_threshold_s: float = 0.0

# Counters (read by stats(); written by the watchdog thread only).
_stalls = 0
_stall_seconds_total = 0.0
_last_stall_s = 0.0
_max_stall_s = 0.0


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def stats() -> dict:
    """Cheap snapshot for ``/health`` and tests."""
    return {
        "enabled": is_running(),
        "threshold_s": _threshold_s,
        "stalls": _stalls,
        "stall_seconds_total": round(_stall_seconds_total, 3),
        "last_stall_s": round(_last_stall_s, 3),
        "max_stall_s": round(_max_stall_s, 3),
    }


def reset_stats() -> None:
    global _stalls, _stall_seconds_total, _last_stall_s, _max_stall_s
    _stalls = 0
    _stall_seconds_total = 0.0
    _last_stall_s = 0.0
    _max_stall_s = 0.0


async def _tick_loop() -> None:
    """Loop-side heartbeat: stamps ``_last_tick`` every ``_TICK_S`` and records
    the loop thread ident (for ``sys._current_frames``) on its first run."""
    global _last_tick, _loop_thread_ident
    _loop_thread_ident = threading.get_ident()
    _last_tick = time.monotonic()
    try:
        while not _stop.is_set():
            await asyncio.sleep(_TICK_S)
            _last_tick = time.monotonic()
    except asyncio.CancelledError:
        return


def _loop_stack() -> str:
    ident = _loop_thread_ident
    if ident is None:
        return "<loop thread ident unknown>"
    frame = sys._current_frames().get(ident)
    if frame is None:
        return "<loop thread frame unavailable>"
    return "".join(traceback.format_stack(frame, limit=_STACK_FRAMES))


def _watch() -> None:
    global _stalls, _stall_seconds_total, _last_stall_s, _max_stall_s
    stalled_since: float | None = None
    last_report = 0.0
    while not _stop.wait(_TICK_S):
        now = time.monotonic()
        if _last_tick <= 0.0:
            continue  # tick task not started yet
        lag = now - _last_tick - _TICK_S
        if lag >= _threshold_s:
            if stalled_since is None:
                stalled_since = _last_tick + _TICK_S
                last_report = now
                logger.warning(
                    "event loop stalled %.1fs (threshold %.1fs); loop thread stack:\n%s",
                    lag, _threshold_s, _loop_stack(),
                )
            elif now - last_report >= _REPEAT_S:
                last_report = now
                logger.warning(
                    "event loop still stalled (%.1fs); loop thread stack:\n%s",
                    now - stalled_since, _loop_stack(),
                )
        elif stalled_since is not None:
            duration = _last_tick - stalled_since
            stalled_since = None
            _stalls += 1
            _stall_seconds_total += duration
            _last_stall_s = duration
            _max_stall_s = max(_max_stall_s, duration)
            logger.info("event loop recovered after %.1fs stall", duration)


def start(threshold_s: float) -> bool:
    """Start the watchdog on the running loop. Returns False when disabled
    (``threshold_s <= 0``) or already running. Must be called from the loop
    thread (it creates the tick task)."""
    global _thread, _tick_task, _threshold_s, _last_tick
    if threshold_s is None or threshold_s <= 0:
        return False
    with _lock:
        if is_running():
            return False
        _stop.clear()
        _threshold_s = float(threshold_s)
        _last_tick = 0.0
        _tick_task = asyncio.get_running_loop().create_task(
            _tick_loop(), name="loop-watchdog-tick",
        )
        _thread = threading.Thread(target=_watch, name="loop-watchdog", daemon=True)
        _thread.start()
    return True


def stop(join_timeout: float = 2.0) -> None:
    """Flag the thread to stop FIRST (so cancelling the tick task can never
    read as a stall), then cancel the tick task and join. Idempotent."""
    global _thread, _tick_task
    with _lock:
        _stop.set()
        thread, _thread = _thread, None
        task, _tick_task = _tick_task, None
    if task is not None and not task.done():
        task.cancel()
    if thread is not None and thread.is_alive():
        thread.join(timeout=join_timeout)

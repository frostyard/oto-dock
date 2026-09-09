"""PostgreSQL connection pool — shared singleton for all storage modules.

Uses psycopg3 sync pool. All storage functions remain synchronous
(called via ``run_db`` / ``asyncio.to_thread`` from async code).

Event-loop rule (2026-09-04, from the 09-03 stall incident): a store call
must never run ON the event loop thread from a PERIODIC or RECONNECT-STORM
path — a slow disk turns every ``COMMIT`` (WAL fsync) into a full proxy
freeze (no pings served → every satellite / dashboard / phone socket drops at
once → reconnects add more writes). Off-loop DB work goes through
``run_db``, which uses a DEDICATED executor sized BELOW the pool so residual
synchronous callers (threadpool endpoints, one-off reads) always find a free
connection instead of waiting on the pool's 30 s timeout. ``loop_guard`` is
the test-time fence (see ``tests/conftest.py``).
"""

import asyncio
import contextlib
import functools
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg_pool
from psycopg.rows import dict_row


_pool: psycopg_pool.ConnectionPool | None = None
_pool_lock = threading.Lock()

# Connections kept OUT of the DB executor's reach: residual sync callers on
# the loop / anyio threadpool can always acquire one even while every executor
# worker is parked on a stalled commit.
_POOL_RESERVE = 4
_DB_EXECUTOR_MAX = 6

_db_executor: ThreadPoolExecutor | None = None
_db_executor_lock = threading.Lock()


def pool_max_size() -> int:
    return int(os.environ.get("DB_POOL_MAX_SIZE", "10"))


def get_pool() -> psycopg_pool.ConnectionPool:
    """Return the shared connection pool (lazy init, double-checked locking)."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        import config
        # `open=False` matches the future psycopg_pool default — the
        # constructor stays side-effect-free and we open the pool with an
        # explicit `.open()` call. We want the singleton open at
        # construction (so the first DB request doesn't pay setup cost),
        # but doing it in two steps rather than via the current
        # `open=True` default keeps us aligned with where the library is
        # heading and survives the eventual removal of the legacy default.
        _pool = psycopg_pool.ConnectionPool(
            conninfo=config.DATABASE_URL,
            min_size=2,
            max_size=pool_max_size(),
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=False,
        )
        _pool.open()
        return _pool


def get_conn():
    """Return a context-managed connection from the pool.

    Usage:
        with get_conn() as conn:
            conn.execute("SELECT ...", (param,))
            conn.commit()

    On normal exit the connection is returned to the pool.
    On exception the transaction is rolled back automatically.
    """
    if _guard_thread_ident is not None or _GUARD_MODE:
        _check_loop_guard()
    return get_pool().connection()


def close_pool(timeout: float = 3.0) -> None:
    """Close the pool at shutdown (idempotent). Its worker/scheduler threads
    are non-daemon — left open they stall interpreter exit until the atexit
    thread join, after uvicorn already logged its shutdown. A later
    ``get_pool()`` call would lazily re-create the pool, so this must be one
    of the LAST shutdown steps."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        with contextlib.suppress(Exception):
            pool.close(timeout=timeout)


# ---------------------------------------------------------------------------
# Dedicated DB executor + run_db
# ---------------------------------------------------------------------------

def db_executor_workers() -> int:
    """Worker count for the DB executor: at most ``_DB_EXECUTOR_MAX`` and
    always ``_POOL_RESERVE`` below the pool size (invariant: a fully busy
    executor can never exhaust the pool)."""
    return max(1, min(_DB_EXECUTOR_MAX, pool_max_size() - _POOL_RESERVE))


def db_executor() -> ThreadPoolExecutor:
    """The dedicated thread pool for off-loop DB work (lazy singleton)."""
    global _db_executor
    if _db_executor is not None:
        return _db_executor
    with _db_executor_lock:
        if _db_executor is None:
            _db_executor = ThreadPoolExecutor(
                max_workers=db_executor_workers(), thread_name_prefix="db",
            )
        return _db_executor


async def run_db(fn, /, *args, **kwargs):
    """Run a synchronous store function on the dedicated DB executor and
    await its result. The ONLY sanctioned way to call a store from a
    periodic loop, a WebSocket handler or any reconnect-storm path."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        db_executor(), functools.partial(fn, *args, **kwargs),
    )


def shutdown_db_executor(*, wait: bool = False) -> None:
    """Stop the DB executor at shutdown (idempotent). Pending futures are
    cancelled; a worker parked on a stalled commit is left to finish so the
    interpreter's exit failsafe (startup.py) decides, not us."""
    global _db_executor
    with _db_executor_lock:
        ex, _db_executor = _db_executor, None
    if ex is not None:
        with contextlib.suppress(Exception):
            ex.shutdown(wait=wait, cancel_futures=True)


# ---------------------------------------------------------------------------
# Loop-thread guard (tests + one-off inventory; never armed in production)
# ---------------------------------------------------------------------------
#
# Two mechanisms:
#   * ``arm_loop_guard()`` / ``loop_guard()`` — pin ONE thread ident (the test
#     loop thread); ``get_conn()`` on that thread raises. Exact, no probing,
#     no false positives from the direct-LLM helper loop or APScheduler jobs.
#     Used by the ``loop_db_guard`` pytest fixture around the exercised call.
#   * ``OTODOCK_DB_LOOP_GUARD=count`` — inventory mode: every ``get_conn()``
#     issued while an event loop is running on the calling thread records the
#     first caller frame outside ``storage/``. ``OTODOCK_DB_LOOP_GUARD=raise``
#     raises instead. Both probe ``asyncio.get_running_loop()`` and are for
#     one-off audit runs only.

_GUARD_MODE = os.environ.get("OTODOCK_DB_LOOP_GUARD", "").strip().lower()
_guard_thread_ident: int | None = None
_guard_hits: dict[tuple[str, int, str], int] = {}
_guard_lock = threading.Lock()


class LoopGuardViolation(RuntimeError):
    """A store was called on the event loop thread while the guard was armed."""


def arm_loop_guard(thread_ident: int | None = None) -> None:
    global _guard_thread_ident
    _guard_thread_ident = thread_ident if thread_ident is not None else threading.get_ident()


def disarm_loop_guard() -> None:
    global _guard_thread_ident
    _guard_thread_ident = None


@contextlib.contextmanager
def loop_guard():
    """Arm the guard for the CURRENT thread for the duration of the block."""
    global _guard_thread_ident
    prev = _guard_thread_ident
    arm_loop_guard()
    try:
        yield
    finally:
        _guard_thread_ident = prev


def loop_guard_hits() -> dict[tuple[str, int, str], int]:
    """Inventory recorded in ``count`` mode: {(file, line, function): n}."""
    with _guard_lock:
        return dict(_guard_hits)


def _caller_outside_storage() -> tuple[str, int, str]:
    frame = sys._getframe(2)
    while frame is not None:
        fname = frame.f_code.co_filename
        if os.sep + "storage" + os.sep not in fname and "storage/pg.py" not in fname:
            return (fname, frame.f_lineno, frame.f_code.co_name)
        frame = frame.f_back
    return ("?", 0, "?")


def _check_loop_guard() -> None:
    if _guard_thread_ident is not None and threading.get_ident() == _guard_thread_ident:
        f, line, fn = _caller_outside_storage()
        raise LoopGuardViolation(
            f"blocking DB call on the event loop thread: {f}:{line} ({fn})"
        )
    if _GUARD_MODE in ("count", "raise", "1"):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # not a loop thread
        hit = _caller_outside_storage()
        if _GUARD_MODE == "count":
            with _guard_lock:
                _guard_hits[hit] = _guard_hits.get(hit, 0) + 1
            return
        raise LoopGuardViolation(
            f"blocking DB call on an event loop thread: {hit[0]}:{hit[1]} ({hit[2]})"
        )

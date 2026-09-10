"""Resource-oriented concurrency admission for locally-executed sessions.

Admission is a **two-gate** test (NOT a static count ceiling). Every *local*
session process-tree (chat / phone / interactive-CLI / meeting participant / task)
reserves a coarse per-TYPE memory estimate, and a new local session is admitted
iff BOTH gates pass:

  GATE 1 (reservation budget):  reserved + est ≤ budget        (budget = total_RAM × BUDGET_FRACTION)
  GATE 2 (live-RAM veto):       live_available − est ≥ FLOOR

Gate 1 is instant + deterministic — its running ``reserved`` sum bounds a burst
(no grow-in window needed). Gate 2 reads the *real* free RAM (cgroup/proc,
reclaimable-aware) so small boxes pack to real capacity and the box can't OOM
under non-session pressure or estimate error. ``est`` is LIGHT for Direct-LLM
(no CLI process) else HEAVY; it counts only the proxy tree (CLI + STDIO MCP
children) — Docker MCPs (sibling containers) and remote MCPs (HTTP) add ~0.

**Remote sessions never count** — ``acquire(target=<machine_id>)`` returns
immediately, untracked (the satellite enforces its own budget).

**Graceful eviction under pressure**: every denied interactive admit runs the
eviction loop, which frees BOTH gates (an evicted session returns budget to
Gate 1 and real RAM to Gate 2). Which gate binds is NOT the attribution signal
by itself: on an uncapped small host the Gate-1 budget is a fraction of HOST
RAM shared with sidecar containers and may never fill, so genuine session
pressure surfaces as a Gate-2 veto. Attribution instead asks "do tracked
sessions plausibly account for the missing RAM?" (``reserved ≥ shortfall``):
yes → evict idle sessions / report "busy"; no → evicting users can't close the
gap → stop and report "host_memory". (2026-07-06 audit: the previous
gate-identity attribution inverted into the OPPOSITE lie on small hosts.)

**Last-resort admit**: the veto never locks an EMPTY platform out — with zero
tracked local sessions the one new session is admitted over both gates (loudly
logged). One swap-backed slow session beats a user locked out of their own box.

**Grow-in debit**: Gate 2 debits the un-materialized remainder of recently
admitted sessions (linear decay over ``_GROW_WINDOW_S``) — N near-simultaneous
warmups otherwise all read the same free RAM before any of them grows into it.

**Denials carry their reason**: ``acquire()`` returns an :class:`Admission`
(truthy = admitted) with ``reason`` ("busy" = sessions genuinely fill the box;
"host_memory" = something ELSE eats host RAM) + ``user_message`` — the text
must not blame "too many active sessions" when the admin page truthfully shows
zero, nor blame the host when idle sessions hold the RAM. Every denial surface
shows ``Admission.user_message`` instead of a hardcoded guess.

State: ``_sessions`` (sid→kind) + ``_session_est`` (sid→MB) + ``_reserved_mb``,
guarded by one ``asyncio.Condition``. One entry per id ⇒ no double-count.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import NamedTuple

import config
from core.sandbox import host_resources

logger = logging.getLogger("claude-proxy.concurrency")

# Surface kinds tracked in _sessions (phone = live phone calls).
_SURFACES = ("chat", "task", "meeting", "phone")

# Reconciler grace: a sid added more recently than this is spared (may be mid-spawn).
_RECONCILE_GRACE_S = 120
# Maintenance loop tick (parked-task wakeups + task-side eviction).
_MAINTENANCE_INTERVAL_S = 30
# Live-RAM read cache TTL — a burst of admits under _cond reads the cgroup once.
_LIVE_CACHE_TTL_S = 1.0
# Grow-in window: a freshly admitted session's RSS takes this long to
# materialize; until then Gate 2 debits the un-grown remainder of its estimate
# (linear decay). Without the debit, N near-simultaneous warmups all read the
# same pre-growth free RAM and overcommit a small box.
_GROW_WINDOW_S = 90.0

# Map an eviction source to the execution_path that resolves its layer.
_EVICT_CLOSE = {"cli": "claude-code-cli", "direct": "direct-llm", "codex": "codex-cli"}


class Admission(NamedTuple):
    """Result of a local-slot admission attempt. Truthy iff admitted, so
    gate-only call sites keep reading naturally (``if not await acquire...``);
    denial surfaces show ``user_message`` (and log ``reason``) instead of
    guessing why."""
    ok: bool
    reason: str | None = None        # "busy" | "host_memory" (None when admitted)
    user_message: str | None = None  # ready-to-display denial text (None when admitted)

    def __bool__(self) -> bool:  # a NamedTuple is otherwise always truthy
        return self.ok


_ADMITTED = Admission(True)


def _deny_busy() -> Admission:
    return Admission(
        False, "busy",
        "Too many active sessions — platform busy. Try again shortly, or close an idle chat.",
    )


def _deny_host_memory(est: int) -> Admission:
    # NOT session pressure: name the real cause, or the message contradicts the
    # admin page (which can truthfully show 0 active sessions).
    return Admission(
        False, "host_memory",
        f"The platform host is low on memory ({_live_available_mb()} MB free; about "
        f"{est + _floor_mb} MB needed to start). This is host memory pressure, not "
        "session count — free memory on the host machine, then retry. "
        "Details: Admin Settings → Platform.",
    )

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_sessions: dict[str, str] = {}            # session_id -> surface kind
_session_est: dict[str, int] = {}         # session_id -> reserved estimate (MB)
_session_added_at: dict[str, float] = {}  # session_id -> time.monotonic() at acquire
_reserved_mb: int = 0                     # cached Σ _session_est
_parked_tasks: int = 0                    # count of tasks blocked in acquire

_cond: asyncio.Condition | None = None    # initialized in init()

_budget_mb: int = 0                       # GATE-1 budget (total × BUDGET_FRACTION), cached at init
_total_mb: int = 0                        # container memory limit (for the gauge)
_floor_mb: int = 0                        # GATE-2 floor, cached at init

_live_cache: tuple[float, int] | None = None   # (monotonic_ts, available_mb)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init() -> None:
    """Create the condition and cache the budget/floor. Call once at startup."""
    global _cond, _budget_mb, _total_mb, _floor_mb

    _cond = asyncio.Condition()
    _total_mb = max(1, host_resources.detect_memory_limit_bytes() // (1024 * 1024))
    _budget_mb = int(_total_mb * config.BUDGET_FRACTION)
    _floor_mb = max(config.SESSION_RESERVE_FLOOR_MB, int(_total_mb * 0.03))

    logger.info(
        "Concurrency: budget=%dMB (%.0f%% of %dMB) · gate-2 floor=%dMB · est heavy/light=%d/%d MB "
        "· evict_floor=%ds · hard_cap=%s",
        _budget_mb, config.BUDGET_FRACTION * 100, _total_mb, _floor_mb,
        config.SESSION_EST_HEAVY_MB, config.SESSION_EST_LIGHT_MB, config.SESSION_EVICT_FLOOR_S,
        config.OTODOCK_MAX_LOCAL_SESSIONS or "off",
    )


# ---------------------------------------------------------------------------
# Estimates + gates (call gate helpers under _cond)
# ---------------------------------------------------------------------------

def _estimate_mb(execution_path: str | None) -> int:
    """Coarse per-TYPE memory reserve. Direct-LLM has no CLI process ⇒ LIGHT."""
    if execution_path == "direct-llm":
        return config.SESSION_EST_LIGHT_MB
    return config.SESSION_EST_HEAVY_MB


def _live_available_mb() -> int:
    """Live allocatable RAM (MB), cached ~1 s. Fail-closed: 0 on read failure."""
    global _live_cache
    now = time.monotonic()
    if _live_cache is not None and now - _live_cache[0] < _LIVE_CACHE_TTL_S:
        return _live_cache[1]
    try:
        mb = host_resources.live_available_bytes() // (1024 * 1024)
        # Swap credit (see config.SESSION_SWAP_CREDIT_MB): MemAvailable counts
        # zero swap; credit half the free swap, capped, so a small box with
        # swap packs one more session by paging cold heap instead of denying.
        # Never credits a fail-closed 0 read ("we know nothing" stays 0).
        if mb:
            mb += _swap_credit_mb()
    except Exception:
        mb = 0
    _live_cache = (now, mb)
    return mb


def _swap_credit_mb() -> int:
    """min(SwapFree / 2, SESSION_SWAP_CREDIT_MB) — 0 when disabled/swapless."""
    if config.SESSION_SWAP_CREDIT_MB <= 0:
        return 0
    swap_mb = host_resources.swap_free_bytes() // (1024 * 1024)
    return min(swap_mb // 2, config.SESSION_SWAP_CREDIT_MB)


def _gate1(est: int, *, is_task: bool) -> bool:
    """Reservation budget (instant). Tasks keep one HEAVY of headroom so background
    work never consumes the room a human interactive session needs."""
    cap = config.OTODOCK_MAX_LOCAL_SESSIONS
    if cap and len(_sessions) >= cap:
        return False
    headroom = config.SESSION_EST_HEAVY_MB if is_task else 0
    return _reserved_mb + est <= _budget_mb - headroom


def _growin_debit_mb() -> int:
    """Σ un-materialized estimate of sessions admitted < ``_GROW_WINDOW_S`` ago.

    Linear decay: a session admitted N seconds ago is assumed to have grown
    into ``est × N/window`` of its reservation already (visible in live RAM),
    so only the remainder is debited — conservative during the window, zero
    after it (no steady-state double-count)."""
    now = time.monotonic()
    debit = 0
    for sid, added in _session_added_at.items():
        elapsed = now - added
        if elapsed < _GROW_WINDOW_S:
            debit += int(_session_est.get(sid, 0) * (1.0 - elapsed / _GROW_WINDOW_S))
    return debit


def _gate2(est: int, *, is_task: bool) -> bool:
    """Live-RAM OOM veto (grow-in-debited). Same task headroom as gate 1."""
    headroom = config.SESSION_EST_HEAVY_MB if is_task else 0
    return _live_available_mb() - _growin_debit_mb() - est >= _floor_mb + headroom


def _has_room(est: int, *, is_task: bool) -> bool:
    return _gate1(est, is_task=is_task) and _gate2(est, is_task=is_task)


def _add(session_id: str, kind: str, est: int) -> None:
    """Reserve a slot. est is written ONLY here (never on idempotent re-acquire)."""
    global _reserved_mb
    assert session_id not in _session_est, "double _add for %s" % session_id
    _sessions[session_id] = kind
    _session_est[session_id] = est
    _session_added_at[session_id] = time.monotonic()
    _reserved_mb += est


def _remove(session_id: str) -> str | None:
    """Single teardown — pops _sessions/_session_est/_session_added_at + frees the
    reservation. Returns the prior kind, or None if it wasn't tracked."""
    global _reserved_mb
    kind = _sessions.pop(session_id, None)
    est = _session_est.pop(session_id, None)
    _session_added_at.pop(session_id, None)
    if est:
        _reserved_mb = max(0, _reserved_mb - est)
    return kind


# ---------------------------------------------------------------------------
# Notify plumbing (sync release/maintenance → locked notify_all)
# ---------------------------------------------------------------------------

async def _notify_waiters() -> None:
    if _cond is None:
        return
    async with _cond:
        _cond.notify_all()


def _schedule_notify() -> None:
    """Hand a lock-held notify to the running loop from a synchronous caller.
    Done UNCONDITIONALLY whenever a slot frees (gating on 'are there waiters?'
    without the lock races a task mid-suspend in wait_for and loses the wakeup)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("concurrency: release outside a running loop; notify skipped")
        return
    loop.create_task(_notify_waiters())


# ---------------------------------------------------------------------------
# Acquire / release
# ---------------------------------------------------------------------------

async def acquire(session_id: str, kind: str, *, target: str = "local",
                  execution_path: str | None = None, blocking: bool = False,
                  user_sub: str | None = None) -> Admission:
    """Acquire a local-session slot.

    Returns an :class:`Admission` (truthy = admitted) — admitted immediately
    when ``target != "local"`` (satellite-budgeted, not counted) or the id is
    already tracked (idempotent). ``blocking`` (tasks) wait until both gates
    allow; interactive admits check-and-reject, but on a Gate-1 (budget) failure
    first try to evict an idle local session to make room. A denial carries
    ``reason`` + ``user_message`` ("busy" = sessions fill the budget/cap;
    "host_memory" = the Gate-2 live-RAM veto, i.e. NON-session pressure).
    """
    if target != "local":
        return _ADMITTED

    assert _cond is not None, "concurrency.init() not called"
    est = _estimate_mb(execution_path)

    if blocking:
        return await _acquire_task(session_id, kind, est)

    async with _cond:
        if session_id in _sessions:
            return _ADMITTED
        if _has_room(est, is_task=False):
            _add(session_id, kind, est)
            logger.debug("Slot acquired: %s (%s) reserved=%d/%dMB", session_id[:8], kind,
                         _reserved_mb, _budget_mb)
            return _ADMITTED

    # Denied at first look. The eviction loop owns what happens next — it
    # frees BOTH gates (budget + real RAM), decides whether tracked sessions
    # plausibly account for the pressure (which gate binds is NOT the signal
    # by itself — see the module docstring), applies the empty-platform
    # last-resort admit, and attributes the eventual denial honestly.
    return await _admit_with_eviction(session_id, kind, est, prefer_user=user_sub)


async def _acquire_task(session_id: str, kind: str, est: int) -> Admission:
    """Blocking task acquire — waits until both gates allow (with HEAVY headroom).
    Task-side eviction is driven by the maintenance loop (keeps wait_for pure)."""
    global _parked_tasks
    async with _cond:
        if session_id in _sessions:
            return _ADMITTED
        if not _has_room(est, is_task=True):
            _parked_tasks += 1
            try:
                await _cond.wait_for(
                    lambda: session_id in _sessions or _has_room(est, is_task=True))
            finally:
                _parked_tasks -= 1  # decrement even on Cancelled/Timeout
        if session_id in _sessions:
            return _ADMITTED
        _add(session_id, kind, est)
        return _ADMITTED


def _pressure_snapshot(est: int) -> tuple[bool, int, bool]:
    """(gate1_ok, live_mb, sessions_account) — call under ``_cond``.

    ``sessions_account`` answers "could evicting tracked sessions plausibly
    close the Gate-2 gap?": the reservation sum covers the live shortfall
    (estimate + floor + grow-in debit − live). A negative shortfall (Gate 2
    passing) trivially accounts."""
    live = _live_available_mb()
    shortfall = (est + _floor_mb + _growin_debit_mb()) - live
    return _gate1(est, is_task=False), live, _reserved_mb >= shortfall


async def _admit_with_eviction(session_id: str, kind: str, est: int, *,
                               prefer_user: str | None) -> Admission:
    """Loop: evict the most-idle idle local session → re-check → admit.

    Runs for EVERY denied interactive admit. Eviction frees BOTH gates (budget
    AND real RAM), so it proceeds while tracked sessions plausibly account for
    the pressure; the loop stops — without sacrificing sessions pointlessly —
    the moment they can't (``host_memory``), admits last-resort on an empty
    platform, and otherwise runs until admitted or nothing idle remains
    (``busy``: sessions genuinely fill the box, none reclaimable)."""
    floor_age = min(config.SESSION_EVICT_FLOOR_S, config.get_idle_timeout())
    while True:
        async with _cond:
            if session_id in _sessions:
                return _ADMITTED
            if _has_room(est, is_task=False):
                _add(session_id, kind, est)
                return _ADMITTED
            gate1_ok, live, sessions_account = _pressure_snapshot(est)
            if not _sessions:
                # LAST-RESORT ADMIT: zero tracked sessions, so the veto is
                # denying the platform its ONLY session — swap can back one
                # (a slow session beats a locked-out user). By construction
                # this bypasses both gates at most once.
                _add(session_id, kind, est)
                logger.warning(
                    "Concurrency: last-resort admit %s (%s) on a low-memory host "
                    "(live=%dMB, est=%dMB, floor=%dMB) — only session, swap-backed",
                    session_id[:8], kind, live, est, _floor_mb,
                )
                return _ADMITTED
            if gate1_ok and not sessions_account:
                # Only Gate 2 binds AND tracked sessions can't plausibly hold
                # the missing RAM → true non-session pressure; evicting user
                # sessions would sacrifice them without closing the gap.
                logger.warning(
                    "Slot denied (live-RAM veto, not session pressure): %s "
                    "reserved=%d/%dMB live=%dMB floor=%dMB",
                    session_id[:8], _reserved_mb, _budget_mb, live, _floor_mb,
                )
                return _deny_host_memory(est)
        victim = await _oldest_evictable_local(floor_age, prefer_user=prefer_user)
        if victim is None:
            break  # nothing idle to reclaim → attribute + deny below
        await _evict_one(*victim)

    async with _cond:
        gate1_ok, live, sessions_account = _pressure_snapshot(est)
    if gate1_ok and not sessions_account:
        logger.warning(
            "Slot denied (live-RAM veto after eviction): %s reserved=%d/%dMB live=%dMB floor=%dMB",
            session_id[:8], _reserved_mb, _budget_mb, live, _floor_mb,
        )
        return _deny_host_memory(est)
    # Sessions fill the budget/cap, or ACTIVE (non-idle) sessions hold the
    # RAM — either way the platform is genuinely busy with session load.
    logger.warning("Slot denied (platform busy): %s (%s) reserved=%d/%dMB live=%dMB",
                   session_id[:8], kind, _reserved_mb, _budget_mb, live)
    return _deny_busy()


def release(session_id: str) -> None:
    """Release a slot. Synchronous + idempotent — safe from reapers, finally
    blocks, the shutdown handler, and multiple cleanup paths for the same id."""
    if _remove(session_id) is None:
        return  # never tracked (e.g. remote) or already released
    _schedule_notify()


# --- Back-compat shims (call sites keep their existing names) ---------------

async def acquire_chat_slot(session_id: str, *, kind: str = "chat",
                            target: str = "local", execution_path: str | None = None,
                            user_sub: str | None = None) -> Admission:
    """Interactive-surface acquire (chat / phone / interactive-CLI)."""
    return await acquire(session_id, kind, target=target, execution_path=execution_path,
                         blocking=False, user_sub=user_sub)


def release_chat_slot(session_id: str) -> None:
    release(session_id)


@asynccontextmanager
async def task_slot(session_id: str, *, target: str = "local",
                    execution_path: str | None = None):
    """Background-task slot — blocking acquire (both gates + HEAVY headroom), then
    always releases on exit. A remote task consumes no local slot and never blocks."""
    await acquire(session_id, "task", target=target, execution_path=execution_path,
                  blocking=True)
    try:
        yield
    finally:
        release(session_id)


async def acquire_meeting_slots(session_ids: list[str], *,
                                targets: dict[str, str] | None = None,
                                exec_paths: dict[str, str] | None = None) -> Admission:
    """Atomic-N reserve of the LOCAL participants against both gates. ``exec_paths``
    maps session_id → execution_path (for the per-type estimate; absent ⇒ HEAVY).
    Meetings deny-without-evicting (v1); the denial carries reason+message like
    ``acquire()``. Releasing all ids later is safe (per-id no-op for the
    untracked remote ones)."""
    assert _cond is not None, "concurrency.init() not called"
    targets = targets or {}
    exec_paths = exec_paths or {}
    local_new = [sid for sid in session_ids
                 if targets.get(sid, "local") == "local" and sid not in _sessions]
    total_est = sum(_estimate_mb(exec_paths.get(sid)) for sid in local_new)
    async with _cond:
        cap = config.OTODOCK_MAX_LOCAL_SESSIONS
        if cap and len(_sessions) + len(local_new) > cap:
            logger.warning("Meeting denied (hard cap): %d local participants", len(local_new))
            return _deny_busy()
        if _reserved_mb + total_est > _budget_mb:
            logger.warning("Meeting denied (budget): need %dMB, reserved=%d/%dMB",
                           total_est, _reserved_mb, _budget_mb)
            return _deny_busy()
        if not _gate2(total_est, is_task=False):
            logger.warning("Meeting denied (live-RAM veto): need %dMB, live=%dMB floor=%dMB",
                           total_est, _live_available_mb(), _floor_mb)
            return _deny_host_memory(total_est)
        for sid in local_new:
            _add(sid, "meeting", _estimate_mb(exec_paths.get(sid)))
        return _ADMITTED


def release_meeting_slots(session_ids: list[str]) -> None:
    for sid in session_ids:
        release(sid)


# ---------------------------------------------------------------------------
# Graceful LRU eviction
# ---------------------------------------------------------------------------

async def _oldest_evictable_local(min_idle_s: float, *,
                                  prefer_user: str | None = None) -> tuple[str, str, bool] | None:
    """Best eviction victim as ``(sid, source, is_prewarm)``, or None.

    Scans the 4 real session pools (cli/direct/codex layer pools + interactive
    locals) reading each ``session.last_activity``. A candidate must hold a
    reservation (be in ``_sessions``) and be idle ≥ ``min_idle_s`` (streaming
    sessions have idle-age ≈ 0 ⇒ auto-excluded). Ordering: unclaimed **pre-warms
    first** (speculative, unused) → the **requesting user's own** idle sessions →
    everyone else; within a group, most-idle first.

    Owned engines are deliberately excluded: their generic cold-resume path
    is not qualified. Ownership persists through startup and failed cleanup,
    even when a stale legacy pool entry has the same session ID.
    """
    from core.session.owned_sessions import get_owned_session

    now = time.monotonic()
    try:
        from core.session.prewarm_session_registry import _entries as _pw_entries
    except Exception:
        _pw_entries = {}
    cands: list[tuple[int, float, str, str, bool]] = []

    def consider(sid: str, s: object, source: str) -> None:
        if sid not in _sessions or get_owned_session(sid) is not None:
            return  # owned engines are not eligible for generic eviction
        age = now - getattr(s, "last_activity", now)
        if age < min_idle_s:
            return
        is_pw = sid in _pw_entries
        owner = getattr(s, "user_sub", None)  # only interactive sessions expose this
        group = 0 if is_pw else (1 if (prefer_user and owner == prefer_user) else 2)
        cands.append((group, -age, sid, source, is_pw))

    try:
        from core.layers.cli.session import _persistent_sessions, _persistent_sessions_lock
        async with _persistent_sessions_lock:
            for sid, s in list(_persistent_sessions.items()):
                consider(sid, s, "cli")
    except Exception:
        pass
    try:
        from core.layers.direct.session import _direct_sessions, _direct_sessions_lock
        async with _direct_sessions_lock:
            for sid, s in list(_direct_sessions.items()):
                consider(sid, s, "direct")
    except Exception:
        pass
    try:
        from core.layers.codex.session import _codex_sessions, _codex_sessions_lock
        async with _codex_sessions_lock:
            for sid, s in list(_codex_sessions.items()):
                consider(sid, s, "codex")
    except Exception:
        pass
    try:
        from core.session import interactive_session as _is
        for sid in _is.live_session_ids(local_only=True):
            s = _is.get(sid)
            if s is not None:
                consider(sid, s, "interactive")
    except Exception:
        pass

    if not cands:
        return None
    cands.sort()  # (group asc, -age asc ⇒ most-idle first within a group)
    _, _, sid, source, is_pw = cands[0]
    return sid, source, is_pw


async def _evict_one(sid: str, source: str, is_prewarm: bool = False) -> bool:
    """Free the reservation under _cond, then close the real session OUTSIDE _cond
    (close is slow). The later release() from close_session is a no-op (already
    removed). Existing dead-session resume covers the evicted-user-returns race.
    A new owned generation invalidates even a previously selected legacy victim.
    """
    from core.session.owned_sessions import get_owned_session

    if get_owned_session(sid) is not None:
        return False
    if is_prewarm:
        # Atomically take the pre-warm out of the reapable set so the reuse path
        # in _spawn_tail can't adopt the session we're about to kill. If someone
        # else just claimed it (for reuse), don't evict — it's a real session now.
        try:
            from core.session.prewarm_session_registry import claim
            if not await claim(sid):
                return False
        except Exception:
            pass
    async with _cond:
        if sid not in _sessions or get_owned_session(sid) is not None:
            return False
        _remove(sid)
    logger.info("Concurrency: evicting idle %ssession %s (%s) to admit a new one",
                "pre-warm " if is_prewarm else "", sid[:8], source)
    try:
        if source == "interactive":
            from core.session import interactive_session as _is
            await _is.close_session(sid, reason="evicted_for_capacity")
        else:
            from core.session.session_manager import get_layer_by_path
            await get_layer_by_path(_EVICT_CLOSE[source]).close_session(sid)
    except Exception as e:
        logger.warning("Concurrency: eviction close failed for %s (%s): %s", sid[:8], source, e)
    # The victim's RSS is freed now — drop the ≤1s live-RAM cache so the
    # eviction loop's immediate re-check reads reality instead of evicting a
    # second session against a stale pre-close number.
    global _live_cache
    _live_cache = None
    if _cond is not None:
        async with _cond:
            _cond.notify_all()
    return True


# ---------------------------------------------------------------------------
# Stats (admin API)
# ---------------------------------------------------------------------------

def get_stats() -> dict:
    """Live concurrency snapshot for the admin dashboard (memory-oriented)."""
    by_surface = {s: 0 for s in _SURFACES}
    for k in _sessions.values():
        by_surface[k] = by_surface.get(k, 0) + 1
    avail = _live_available_mb()
    budget_headroom = max(0, _budget_mb - _reserved_mb)
    live_headroom = max(0, avail - _floor_mb)
    heavy = max(1, config.SESSION_EST_HEAVY_MB)
    light = max(1, config.SESSION_EST_LIGHT_MB)
    return {
        "sessions": {
            "active": len(_sessions),
            "reserved_mb": _reserved_mb,
            "budget_mb": _budget_mb,
            "available_mb": avail,
            "total_mb": _total_mb,
            "fit_heavy": min(budget_headroom // heavy, live_headroom // heavy),
            "fit_light": min(budget_headroom // light, live_headroom // light),
        },
        "tasks": {"active": by_surface.get("task", 0)},
        "by_surface": by_surface,
        "satellites": _satellite_stats(),
    }


def _satellite_stats() -> list[dict]:
    """Per-satellite live counts + load (filled by the satellite layer). Empty
    until the satellite connection manager is up."""
    try:
        from core.remote.satellite_connection import get_connection_manager
        return get_connection_manager().concurrency_stats()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Maintenance loop — parked-task wakeups + task-side eviction
# ---------------------------------------------------------------------------

async def maintenance_loop() -> None:
    """Periodically wake parked tasks (the budget/live-RAM predicate can flip with
    no release — RAM freeing, a session ending elsewhere) and, when a task is
    parked because the budget is full, evict one idle local session to unblock it."""
    while True:
        await asyncio.sleep(_MAINTENANCE_INTERVAL_S)
        try:
            if _parked_tasks <= 0 or _cond is None:
                continue
            async with _cond:
                budget_full = not _gate1(config.SESSION_EST_HEAVY_MB, is_task=True)
            if budget_full:
                floor_age = min(config.SESSION_EVICT_FLOOR_S, config.get_idle_timeout())
                victim = await _oldest_evictable_local(floor_age, prefer_user=None)
                if victim is not None:
                    await _evict_one(*victim)
            async with _cond:
                _cond.notify_all()
        except Exception as e:
            logger.error("concurrency maintenance loop error: %s", e)


# ---------------------------------------------------------------------------
# Reconciliation — safety net for orphaned slots
# ---------------------------------------------------------------------------

async def reconcile_chat_slots() -> int:
    """Release orphaned LOCAL slots not backed by any live registry.

    Tasks are EXCLUDED (their task_slot finally is authoritative); remote layer
    sessions are not a live source (they never hold a slot); interactive counted
    LOCAL-only; sids added within the last sweep are spared (mid-spawn window).
    Explicit local owners retain slots until ownership is released, including
    startup, closing and failed cleanup; active admission status is irrelevant.
    """
    from core.layers.cli.session import _persistent_sessions, _persistent_sessions_lock
    from core.layers.direct.session import _direct_sessions, _direct_sessions_lock
    from core.layers.codex.session import _codex_sessions, _codex_sessions_lock
    from core.events.stream_pump import _active_pumps
    from core.session.owned_sessions import owned_session_ids

    live_sids: set[str] = set()
    async with _persistent_sessions_lock:
        live_sids.update(_persistent_sessions.keys())
    async with _direct_sessions_lock:
        live_sids.update(_direct_sessions.keys())
    async with _codex_sessions_lock:
        live_sids.update(_codex_sessions.keys())
    live_sids.update(p.session_id for p in _active_pumps.values() if not p.is_done)

    try:
        from core.session.interactive_session import live_session_ids
        live_sids.update(live_session_ids(local_only=True))
    except Exception:
        pass

    if _cond is None:
        return 0
    now = time.monotonic()
    orphaned: list[str] = []
    async with _cond:
        # Read after the last await: a claim acquired while we waited for the
        # condition must protect its reservation in this same reconciliation.
        live_sids.update(owned_session_ids(local_only=True))
        for sid, kind in list(_sessions.items()):
            if kind == "task":
                continue  # lifecycle owned by task_slot's finally
            if sid in live_sids:
                continue
            if now - _session_added_at.get(sid, 0.0) < _RECONCILE_GRACE_S:
                continue  # mid-spawn — spare it
            orphaned.append(sid)
        for sid in orphaned:
            _remove(sid)
            logger.warning("Reconciliation: released orphaned slot %s (reserved=%d/%dMB)",
                           sid[:8], _reserved_mb, _budget_mb)
        if orphaned:
            _cond.notify_all()  # we already hold the lock
    return len(orphaned)


async def _reconciliation_loop() -> None:
    """Background task: periodically reconcile local slots."""
    while True:
        await asyncio.sleep(_RECONCILE_GRACE_S)
        try:
            released = await reconcile_chat_slots()
            if released:
                logger.info("Reconciliation released %d orphaned slot(s). reserved=%d/%dMB",
                            released, _reserved_mb, _budget_mb)
        except Exception as e:
            logger.error("Reconciliation error: %s", e)

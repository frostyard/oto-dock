"""Real ownership claims protect accounting and scheduler lanes until release."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

import config
from core import concurrency as manager
from core.session import owned_sessions as ownership


class ObservedCondition(asyncio.Condition):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        return await super().__aenter__()


@pytest.fixture
def harness(monkeypatch):
    # The scheduler's import-time timezone lookup is the only storage seam.
    monkeypatch.setattr(config, "get_platform_timezone", lambda: "UTC")
    from core.events import stream_pump
    from core.layers.cli import session as cli
    from core.layers.codex import session as codex
    from core.layers.direct import session as direct
    from core.session import interactive_session, session_manager
    from services.scheduler import scheduler

    pools = {}
    for name, module, attr in (
        ("cli", cli, "_persistent_sessions"),
        ("codex", codex, "_codex_sessions"),
        ("direct", direct, "_direct_sessions"),
    ):
        pools[name] = {}
        monkeypatch.setattr(module, attr, pools[name])
        monkeypatch.setattr(module, attr + "_lock", asyncio.Lock())
    monkeypatch.setattr(stream_pump, "_active_pumps", {})
    monkeypatch.setattr(interactive_session, "live_session_ids", lambda **kwargs: frozenset())
    monkeypatch.setattr(session_manager, "_remote_layer", None)
    for attr in ("_sessions", "_session_est", "_session_added_at"):
        monkeypatch.setattr(manager, attr, {})
    monkeypatch.setattr(manager, "_reserved_mb", 0)
    condition = ObservedCondition()
    monkeypatch.setattr(manager, "_cond", condition)
    handles = []

    def reserve(sid=None):
        sid = sid or str(uuid.uuid4())
        manager._add(sid, "chat", 1000)
        manager._session_added_at[sid] = time.monotonic() - 10000
        return sid

    def claim(sid, *, local=True, active=False, close=None):
        def admission():
            if active == "error":
                raise AssertionError("Admission must not be consulted by cleanup")
            return active

        handle = ownership.register_owned_session(
            session_id=sid, engine="copilot-cli", agent="fixture", user_sub="user",
            username="fixture", local=local, active=admission, close=close or AsyncMock(),
        )
        handles.append(handle)
        return handle

    yield SimpleNamespace(reserve=reserve, claim=claim, pools=pools, condition=condition,
                          scheduler=scheduler, session_manager=session_manager,
                          pumps=stream_pump._active_pumps)
    for handle in handles:
        ownership.release_owned_session(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False, "error"])
async def test_local_ownership_keeps_aged_slot_until_exact_release(harness, active):
    sid = harness.reserve()
    handle = harness.claim(sid, active=active)
    assert await manager.reconcile_chat_slots() == 0
    assert manager._reserved_mb == 1000
    assert ownership.release_owned_session(handle)
    assert await manager.reconcile_chat_slots() == 1
    assert manager._reserved_mb == 0


@pytest.mark.asyncio
async def test_failed_owner_cleanup_keeps_reservation_and_lane(harness):
    sid = harness.reserve()
    handle = harness.claim(sid, close=AsyncMock(side_effect=RuntimeError("fixture")))
    with pytest.raises(ownership.SessionOwnershipError):
        await handle.close()
    assert not handle.active
    assert await manager.reconcile_chat_slots() == 0
    assert not harness.scheduler._lane_pump_wedged(SimpleNamespace(session_id=sid))


@pytest.mark.asyncio
async def test_closing_owner_keeps_slot_and_lane_until_cleanup_releases(harness):
    sid = harness.reserve()
    started, finish = asyncio.Event(), asyncio.Event()

    async def close():
        started.set()
        await finish.wait()
        ownership.release_owned_session(handle)

    handle = harness.claim(sid, active=True, close=close)
    waiter = asyncio.create_task(handle.close())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not handle.active
        assert await manager.reconcile_chat_slots() == 0
        assert not harness.scheduler._lane_pump_wedged(SimpleNamespace(session_id=sid))
    finally:
        finish.set()
        await asyncio.wait_for(waiter, 1)
    assert await manager.reconcile_chat_slots() == 1
    assert harness.scheduler._lane_pump_wedged(SimpleNamespace(session_id=sid))


@pytest.mark.asyncio
async def test_remote_owner_does_not_keep_mistaken_local_reservation(harness):
    sid = harness.reserve()
    harness.claim(sid, local=False)
    assert await manager.reconcile_chat_slots() == 1
    assert not harness.scheduler._lane_pump_wedged(SimpleNamespace(session_id=sid))


@pytest.mark.asyncio
async def test_reconcile_reads_claim_after_condition_wait(harness):
    sid = harness.reserve()
    async with harness.condition:
        harness.condition.entered.clear()
        task = asyncio.create_task(manager.reconcile_chat_slots())
        await asyncio.wait_for(harness.condition.entered.wait(), 1)
        harness.claim(sid)
    assert await asyncio.wait_for(task, 1) == 0
    assert manager._reserved_mb == 1000


@pytest.mark.asyncio
async def test_failed_inventory_read_never_releases_slots(harness, monkeypatch):
    sid = harness.reserve()

    def unavailable(**kwargs):
        raise RuntimeError("inventory unavailable")

    monkeypatch.setattr(ownership, "owned_session_ids", unavailable)
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        await manager.reconcile_chat_slots()
    assert sid in manager._sessions
    assert manager._reserved_mb == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["cli", "codex", "direct"])
async def test_owned_id_in_stale_legacy_pool_is_not_lru_candidate(harness, source):
    protected, ordinary = harness.reserve(), harness.reserve()
    handle = harness.claim(protected)
    harness.pools[source].update({
        protected: SimpleNamespace(last_activity=time.monotonic() - 10000),
        ordinary: SimpleNamespace(last_activity=time.monotonic() - 1000),
    })
    assert await manager._oldest_evictable_local(10) == (ordinary, source, False)
    ownership.release_owned_session(handle)
    assert await manager._oldest_evictable_local(10) == (protected, source, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("during_wait", [False, True])
async def test_stale_eviction_rechecks_new_owner_before_remove(harness, during_wait):
    sid = harness.reserve()
    close = AsyncMock()
    if during_wait:
        async with harness.condition:
            harness.condition.entered.clear()
            task = asyncio.create_task(manager._evict_one(sid, "cli"))
            await asyncio.wait_for(harness.condition.entered.wait(), 1)
            harness.claim(sid, close=close)
        result = await asyncio.wait_for(task, 1)
    else:
        harness.claim(sid, close=close)
        result = await manager._evict_one(sid, "cli")
    assert result is False
    assert manager._reserved_mb == 1000
    assert sid in manager._sessions
    close.assert_not_awaited()


@pytest.mark.parametrize("active", [True, False, "error"])
def test_lane_retains_any_owner_even_when_legacy_process_looks_dead(harness, active):
    sid = str(uuid.uuid4())
    harness.pools["cli"][sid] = SimpleNamespace(proc=None)
    handle = harness.claim(sid, active=active)
    pump = SimpleNamespace(session_id=sid)
    assert not harness.scheduler._lane_pump_wedged(pump)
    ownership.release_owned_session(handle)
    assert harness.scheduler._lane_pump_wedged(pump)


@pytest.mark.parametrize("source,alive", [("cli", True), ("cli", False),
                                        ("codex", True), ("direct", True)])
def test_unowned_legacy_lane_behavior_is_preserved(harness, source, alive):
    sid = str(uuid.uuid4())
    harness.pools[source][sid] = SimpleNamespace(proc=SimpleNamespace(returncode=None if alive else 1))
    assert harness.scheduler._lane_pump_wedged(SimpleNamespace(session_id=sid)) is (not alive)


@pytest.mark.parametrize("severed,dead", [(False, False), (True, False), (False, True)])
def test_unowned_remote_lane_behavior_is_preserved(harness, severed, dead):
    sid = str(uuid.uuid4())
    harness.session_manager._remote_layer = SimpleNamespace(
        _sessions={sid: SimpleNamespace(cli_dead=dead)},
        remote_stream_severed=lambda _: severed,
    )
    assert harness.scheduler._lane_pump_wedged(SimpleNamespace(session_id=sid)) is (severed or dead)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_lane_ceiling_cannot_reap_current_owned_pump(harness, monkeypatch, replacement):
    sid, chat_id = str(uuid.uuid4()), str(uuid.uuid4())
    harness.claim(sid)
    original = SimpleNamespace(session_id=sid, is_done=False)
    harness.pumps[chat_id] = original

    async def ceiling(_):
        if replacement:
            new_sid = str(uuid.uuid4())
            harness.claim(new_sid)
            harness.pumps[chat_id] = SimpleNamespace(session_id=new_sid, is_done=False)

    wait = AsyncMock(side_effect=ceiling)
    reap = AsyncMock()
    monkeypatch.setattr(harness.scheduler, "_await_lane_quiescence", wait)
    monkeypatch.setattr(harness.scheduler, "_reap_prior_lane_pump", reap)
    with pytest.raises(RuntimeError, match="^Prior lane still owns runtime resources$"):
        await harness.scheduler._settle_prior_lane(chat_id, "run")
    wait.assert_awaited_once_with(chat_id)
    reap.assert_not_awaited()
    assert (harness.pumps[chat_id] is not original) is replacement


@pytest.mark.asyncio
async def test_lane_after_wait_can_reap_orphan_only_after_claim_released(harness, monkeypatch):
    sid, chat_id = str(uuid.uuid4()), str(uuid.uuid4())
    handle = harness.claim(sid)
    harness.pumps[chat_id] = SimpleNamespace(session_id=sid, is_done=False)

    async def cleanup(_):
        ownership.release_owned_session(handle)

    monkeypatch.setattr(harness.scheduler, "_await_lane_quiescence", cleanup)
    reap = AsyncMock()
    monkeypatch.setattr(harness.scheduler, "_reap_prior_lane_pump", reap)
    assert await harness.scheduler._settle_prior_lane(chat_id, "run") is True
    reap.assert_awaited_once_with(chat_id, "run")

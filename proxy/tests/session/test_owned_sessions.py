"""Runtime ownership, active admission and exact-generation close contracts."""

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from core.session import owned_sessions as registry


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(registry, "_sessions", {})
    monkeypatch.setattr(registry, "_shutting_down", False)


def claim(sid="session", **options):
    async def close():
        registry.release_owned_session(handle)

    handle = registry.register_owned_session(**{
        "session_id": sid, "engine": "fixture-engine", "agent": "demo", "user_sub": "alice",
        "username": "", "active": lambda: True, "close": close, **options,
    })
    return handle


def test_snapshot_does_not_probe_health_and_metadata_is_immutable():
    def unexpected():
        raise AssertionError("inventory must not ask for admission")

    handle = claim(active=unexpected)
    remote = claim("remote", local=False)
    assert registry.owned_session_ids() == {"session", "remote"}
    assert registry.owned_sessions(local_only=True) == (handle,)
    assert registry.owned_session_ids(local_only=True) == {"session"}
    assert not handle.active and remote.active
    assert handle.agent == "demo" and handle.user_sub == "alice" and handle.username == ""
    with pytest.raises(FrozenInstanceError):
        handle.agent = "other-agent"


@pytest.mark.parametrize("active", [None, False, 0, 1, "true", {}, []])
def test_only_exact_true_admits_requests(active):
    handle = claim(active=lambda: active)
    assert not handle.active
    assert registry.get_owned_session("session") is handle


@pytest.mark.parametrize("error", [ValueError, asyncio.CancelledError])
def test_health_failures_deny_without_removing_resources(error):
    def broken():
        raise error("private diagnostic")

    handle = claim(active=broken)
    assert not handle.active and registry.owned_session_ids() == {"session"}


@pytest.mark.parametrize("options", [
    {"session_id": ""}, {"session_id": " id"}, {"session_id": "x" * 257},
    {"engine": "bad\nengine"}, {"agent": None}, {"user_sub": 1}, {"username": "bad\x00name"},
    {"local": 1}, {"active": True}, {"close": None},
])
def test_invalid_claims_have_no_side_effects(options):
    with pytest.raises(registry.SessionOwnershipError):
        claim(**options)
    assert not registry.owned_sessions()


def test_async_health_is_rejected_without_creating_coroutine():
    async def active():
        return True

    with pytest.raises(registry.SessionOwnershipError):
        claim(active=active)


def test_duplicate_does_not_replace_original_or_run_callbacks():
    original = claim()
    with pytest.raises(registry.SessionOwnershipError):
        claim(engine="other")
    assert registry.get_owned_session("session") is original
    assert original.active


def test_health_callback_cannot_admit_a_released_generation():
    def active():
        registry.release_owned_session(handle)
        return True

    handle = claim(active=active)
    assert not handle.active


@pytest.mark.asyncio
async def test_stale_snapshot_never_closes_or_releases_replacement():
    old = claim()
    snapshot = registry.owned_sessions()
    assert registry.release_owned_session(old)
    new = claim()
    assert not await snapshot[0].close()
    assert not registry.release_owned_session(old)
    assert not old.active and new.active
    assert await new.close()
    assert not registry.owned_sessions()


@pytest.mark.asyncio
async def test_multiple_close_waiters_share_work_and_cancellation_does_not_cancel_cleanup():
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def close():
        calls.append(True)
        entered.set()
        await finish.wait()
        registry.release_owned_session(handle)

    handle = claim(close=close)
    first = asyncio.create_task(handle.close())
    await entered.wait()
    second = asyncio.create_task(handle.close())
    assert not handle.active and registry.get_owned_session("session") is handle
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not handle._closing.done()
    finish.set()
    assert await second and calls == [True]
    assert not registry.owned_sessions()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["raise", "cancel", "forgot-release"])
async def test_failed_cleanup_keeps_claim_and_returns_only_sanitized_cached_error(outcome):
    calls = []

    async def close():
        calls.append(True)
        if outcome == "raise":
            raise RuntimeError("secret fixture failure")
        if outcome == "cancel":
            raise asyncio.CancelledError("secret fixture cancellation")

    handle = claim(close=close)
    for _ in range(2):
        with pytest.raises(registry.SessionOwnershipError) as error:
            await handle.close()
        assert "secret" not in str(error.value) and error.value.__context__ is None
    assert calls == [True] and not handle.active
    assert registry.get_owned_session("session") is handle
    with pytest.raises(registry.SessionOwnershipError):
        claim()


@pytest.mark.asyncio
async def test_close_callback_replacement_is_not_released_by_old_completion():
    replacement = []

    async def close():
        registry.release_owned_session(old)
        replacement.append(claim())

    old = claim(close=close)
    assert await old.close()
    assert registry.get_owned_session("session") is replacement[0]
    assert replacement[0].active and not old.active


def test_shutdown_seals_admission_before_snapshot_even_after_prior_claims_release():
    original = claim()
    assert registry.begin_owned_session_shutdown() == (original,)
    assert registry.release_owned_session(original)
    for sid in ("session", "new"):
        with pytest.raises(registry.SessionOwnershipError):
            claim(sid)
    assert registry.begin_owned_session_shutdown() == ()


def test_request_admission_and_foreign_legacy_detection_remain_distinct(monkeypatch):
    from core.session import session_manager
    from core.layers.codex import session as codex

    monkeypatch.setitem(codex._codex_sessions, "session", SimpleNamespace())
    handle = claim(active=lambda: False)
    assert session_manager.has_legacy_session("session")
    assert not session_manager.is_session_registered("session")
    assert registry.release_owned_session(handle)
    assert session_manager.is_session_registered("session")

"""Platform cleanup uses captured owners even after context/config changes.

Real registry and consumer functions; storage reads and unrelated runtimes are
isolated fixtures. No SDK, native process, inference, or database is required.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

from core.session import owned_sessions as registry


@pytest.fixture(autouse=True)
def no_database_timezone(monkeypatch):
    import config

    # Scheduler construction asks for the platform timezone at import time.
    monkeypatch.setattr(config, "get_platform_timezone", lambda: "UTC")


@pytest.fixture
def owned(monkeypatch):
    monkeypatch.setattr(registry, "_shutting_down", False)
    handles = []

    def create(*, session_id=None, active=False, local=True, close=None, username=""):
        calls = []

        async def default_close():
            calls.append("closed")
            registry.release_owned_session(handle)

        handle = registry.register_owned_session(
            session_id=session_id or str(uuid.uuid4()), engine="copilot-cli", agent="captured-agent",
            user_sub="captured-user", username=username, local=local,
            active=lambda: active, close=close or default_close,
        )
        handles.append(handle)
        return handle, calls

    yield create
    for handle in reversed(handles):
        registry.release_owned_session(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_agent", ["", "changed-agent"])
async def test_chat_close_prefers_owned_generation_without_agent_resolution(monkeypatch, owned, stored_agent):
    from api.agents import chats
    from core.session import session_manager

    handle, calls = owned()
    resolver = Mock(side_effect=AssertionError("stored configuration must not resolve"))
    monkeypatch.setattr(session_manager, "get_execution_layer", resolver)
    await chats._close_chat_session({
        "session_id": handle.session_id, "agent": stored_agent, "execution_path": "direct-llm",
    })
    assert calls == ["closed"]
    assert registry.get_owned_session(handle.session_id) is None
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_failed_chat_close_keeps_owner_and_never_falls_back(monkeypatch, owned):
    from api.agents import chats
    from core.session import session_manager
    from fastapi import HTTPException

    close = AsyncMock(side_effect=RuntimeError("fixture cleanup failed"))
    handle, _ = owned(close=close)
    resolver = Mock(side_effect=AssertionError("wrong layer"))
    monkeypatch.setattr(session_manager, "get_execution_layer", resolver)
    with pytest.raises(HTTPException) as error:
        await chats._close_chat_session({"session_id": handle.session_id, "agent": "changed-agent"})
    assert error.value.status_code == 503
    assert error.value.__context__ is None
    assert "fixture cleanup failed" not in error.value.detail
    close.assert_awaited_once()
    resolver.assert_not_called()
    assert registry.get_owned_session(handle.session_id) is handle


@pytest.mark.asyncio
async def test_delete_chat_keeps_row_when_owned_cleanup_fails(monkeypatch, owned):
    from api.agents import chats
    from fastapi import HTTPException

    close = AsyncMock(side_effect=RuntimeError("fixture cleanup failure"))
    handle, _ = owned(close=close)
    chat = {"id": "fixture-chat", "session_id": handle.session_id, "agent": "changed-agent"}
    monkeypatch.setattr(chats, "require_auth", lambda user: user)
    monkeypatch.setattr(chats, "can_access_chat", lambda *_: True)
    monkeypatch.setattr(chats, "can_mutate_chat", lambda *_: True)
    monkeypatch.setattr(chats.task_store, "get_chat", lambda _: chat)
    monkeypatch.setattr(chats.task_store, "has_live_run", lambda _: False)
    monkeypatch.setattr(chats.task_store, "list_continuations_for_chat", lambda _: [])
    delete = Mock()
    monkeypatch.setattr(chats.task_store, "delete_chat", delete)
    with pytest.raises(HTTPException) as error:
        await chats.delete_chat(chat["id"], user=object())
    assert error.value.status_code == 503
    delete.assert_not_called()
    assert registry.get_owned_session(handle.session_id) is handle


@pytest.mark.asyncio
async def test_chat_close_preserves_replacement_claim(owned):
    from api.agents import chats
    from fastapi import HTTPException

    replacements = []

    async def replace_owner():
        registry.release_owned_session(handle)
        replacements.append(owned(session_id=handle.session_id))

    handle, _ = owned(close=replace_owner)
    with pytest.raises(HTTPException) as error:
        await chats._close_chat_session({"session_id": handle.session_id, "agent": "changed-agent"})
    assert error.value.status_code == 503
    replacement, calls = replacements[0]
    assert registry.get_owned_session(handle.session_id) is replacement
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_dashboard_blocks_switch_while_owner_holds_resources(monkeypatch, owned, active):
    from core.session import interactive_session, session_manager
    from ws import dashboard

    handle, _ = owned(active=active)
    monkeypatch.setattr(interactive_session, "find_live_for_chat", lambda _: None)
    resolver = Mock(side_effect=AssertionError("stored configuration must not resolve"))
    monkeypatch.setattr(session_manager, "resolve_execution_path", resolver)
    chat = {"id": "fixture-chat", "session_id": handle.session_id, "agent": "changed-agent",
            "execution_path": "direct-llm"}
    assert await dashboard.chat_process_alive(chat) is True
    resolver.assert_not_called()
    registry.release_owned_session(handle)
    monkeypatch.setattr(session_manager, "resolve_execution_path", lambda *_: "direct-llm")
    assert await dashboard.chat_process_alive(chat) is False


def test_retention_protects_contextless_owned_id_and_home_until_release(monkeypatch, tmp_path, owned):
    from core.session import session_state
    from services.infra import retention

    handle, _ = owned(username="captured-home")
    assert session_state.get_session_security(handle.session_id) is None
    home = tmp_path / "home"
    session_file = home / ".claude/projects/project" / f"{handle.session_id}.jsonl"
    junk_file = home / ".codex/logs_fixture.sqlite"
    for path in (session_file, junk_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
        os.utime(path, (1, 1))
    monkeypatch.setattr(retention.task_store, "get_all_session_refs", set)
    monkeypatch.setattr(retention, "iter_local_homes",
                        lambda: iter([("captured-agent", "captured-home", home)]))
    monkeypatch.setattr(retention.task_store, "get_retention_candidate_chats", lambda _: [{
        "id": "old-chat", "session_id": handle.session_id, "agent": "changed-agent",
    }])
    monkeypatch.setattr(retention.task_store, "get_protected_session_refs", lambda _: (set(), set()))
    flags = Mock()
    monkeypatch.setattr(retention.task_store, "flag_chats_for_retention", flags)
    stats = {key: 0 for key in (
        "orphans_deleted", "orphan_bytes", "codex_junk_files", "codex_junk_bytes", "errors", "chats_flagged",
    )}
    snapshot = retention._build_live_snapshot()
    assert handle.session_id in snapshot.session_ids
    assert ("captured-agent", "captured-home") in snapshot.busy_homes
    retention._pass_aged_chats(30, snapshot, stats, False)
    retention._pass_orphans(snapshot, stats, False)
    retention._pass_codex_junk(snapshot, stats, False)
    flags.assert_not_called()
    assert session_file.exists() and junk_file.exists()
    assert stats["orphans_deleted"] == stats["codex_junk_files"] == 0

    registry.release_owned_session(handle)
    released = retention._build_live_snapshot()
    assert handle.session_id not in released.session_ids
    assert ("captured-agent", "captured-home") not in released.busy_homes
    retention._pass_orphans(released, stats, False)
    retention._pass_codex_junk(released, stats, False)
    assert not session_file.exists() and not junk_file.exists()
    assert stats["orphans_deleted"] == stats["codex_junk_files"] == 1


def test_retention_remote_owner_does_not_protect_unrelated_local_home(owned):
    from services.infra import retention

    handle, _ = owned(local=False, username="remote-home")
    snapshot = retention._build_live_snapshot()
    assert handle.session_id not in snapshot.session_ids
    assert ("captured-agent", "remote-home") not in snapshot.busy_homes


@pytest.fixture
def shutdown_environment(monkeypatch):
    import startup
    from core.layers.cli import session as cli
    from core.layers.codex import session as codex
    from core.layers.direct import session as direct
    from core.session import session_manager
    from services.meetings import meeting_orchestrator

    order = []

    async def stop_scheduler():
        order.append("scheduler")

    async def stop_meetings():
        order.append("meetings")

    monkeypatch.setattr(startup.scheduler, "shutdown", stop_scheduler)
    monkeypatch.setattr(meeting_orchestrator, "shutdown_meetings", stop_meetings)
    for module, prefix in ((cli, "_persistent"), (codex, "_codex"), (direct, "_direct")):
        monkeypatch.setattr(module, prefix + "_sessions", {})
        monkeypatch.setattr(module, prefix + "_sessions_lock", asyncio.Lock())
    resolver = Mock(side_effect=AssertionError("stored configuration must not resolve"))
    monkeypatch.setattr(session_manager, "get_execution_layer", resolver)
    monkeypatch.setattr(session_manager, "_remote_layer", None)
    return SimpleNamespace(startup=startup, session_manager=session_manager, order=order, resolver=resolver)


@pytest.mark.asyncio
async def test_shutdown_closes_local_owned_generations_and_preserves_remote_inflight(
    monkeypatch, owned, shutdown_environment,
):
    env = shutdown_environment

    async def failed_close():
        env.order.append("failed-owner")
        raise RuntimeError("fixture cleanup failure")

    failed, _ = owned(close=failed_close)
    good, calls = owned()
    remote_owner, remote_calls = owned(local=False)
    remote_close = AsyncMock()
    monkeypatch.setattr(env.session_manager, "_remote_layer", SimpleNamespace(
        _sessions={
            "inflight": SimpleNamespace(execution_path="claude-code-cli", turn_active=True),
            "idle": SimpleNamespace(execution_path="claude-code-cli", turn_active=False),
        }, close_session=remote_close,
    ))
    await env.startup._shutdown_sessions(env.startup.logger)
    assert env.order == ["scheduler", "meetings", "failed-owner"]
    assert calls == ["closed"]
    assert registry.get_owned_session(good.session_id) is None
    assert registry.get_owned_session(failed.session_id) is failed
    assert registry.get_owned_session(remote_owner.session_id) is remote_owner
    assert remote_calls == []
    remote_close.assert_awaited_once_with("idle")
    env.resolver.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_blocks_new_claims_before_dependents_drain(monkeypatch, owned, shutdown_environment):
    env = shutdown_environment
    first, calls = owned()
    late_attempts = []

    async def attempt_start_during_scheduler_shutdown():
        with pytest.raises(registry.SessionOwnershipError):
            owned()
        late_attempts.append("rejected")

    monkeypatch.setattr(env.startup.scheduler, "shutdown", attempt_start_during_scheduler_shutdown)
    await env.startup._shutdown_sessions(env.startup.logger)
    assert late_attempts == ["rejected"]
    assert calls == ["closed"]
    assert registry.get_owned_session(first.session_id) is None
    with pytest.raises(registry.SessionOwnershipError):
        owned(session_id=first.session_id)
    env.resolver.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_dispatches_all_owners_before_waiting_for_slow_cleanup(owned, shutdown_environment):
    env = shutdown_environment
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow_close():
        started.set()
        await finish.wait()
        registry.release_owned_session(slow)

    slow, _ = owned(close=slow_close)
    fast, calls = owned()
    task = asyncio.create_task(env.startup._shutdown_sessions(env.startup.logger))
    try:
        await asyncio.wait_for(started.wait(), 1)
        async with asyncio.timeout(1):
            while registry.get_owned_session(fast.session_id) is not None:
                await asyncio.sleep(0)
        assert calls == ["closed"]
        assert registry.get_owned_session(slow.session_id) is slow
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert slow._closing is not None and not slow._closing.done()
    finally:
        finish.set()
        if slow._closing is not None:
            await asyncio.wait_for(asyncio.shield(slow._closing), 1)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert registry.get_owned_session(slow.session_id) is None

"""Personal preview admission, real human queues, and owned producer teardown."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
import uuid
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "copilot"))
from conversation_fixture import MemoryConversations

import pytest
import pytest_asyncio

from auth.providers import UserContext
from core.events.common_events import CommonEvent
from core.session import session_state as state
from services.engines import copilot_chat as module
from services.engines.copilot_chat import CopilotChatError, CopilotChatService


class Layer:
    def __init__(self):
        self.sessions = {}
        self.started, self.closed, self.messages = [], [], []
        self.program = self.start_hook = self.close_hook = None
        self.usage_observers = {}
        self.delegate_handlers = {}

    async def start_session(self, sid, config, *, usage_observer=None, delegate_handler=None):
        self.delegate_handlers[sid] = delegate_handler
        self.usage_observers[sid] = usage_observer
        self.started.append((sid, config))
        self.sessions[sid] = asyncio.Lock()
        if self.start_hook:
            await self.start_hook(sid)

    @asynccontextmanager
    async def session_lock(self, sid):
        async with self.sessions[sid]:
            yield

    async def send_message(self, sid, text):
        self.messages.append((sid, text))
        if self.program:
            async for event in self.program(sid):
                yield event
        else:
            yield CommonEvent("text", {"content": "fixture response"})
            yield CommonEvent("done", {})

    async def close_session(self, sid):
        if self.close_hook:
            await self.close_hook(sid)
        self.closed.append(sid)
        self.sessions.pop(sid, None)
        state.resolve_session_permissions(sid, approved=False)
        state._permission_emitters.pop(sid, None)

    async def is_session_alive(self, sid):
        return sid in self.sessions

    async def is_session_process_dead(self, sid):
        return sid not in self.sessions

    async def is_usage_source_closed(self, sid):
        return sid not in self.sessions

    async def history_ready(self, sid, owner):
        return sid not in self.sessions

    async def respond_permission(self, sid, request_id, approved):
        assert state.get_permission_request_session(request_id) == sid
        assert state.resolve_permission(request_id, approved)

    async def aclose(self):
        await asyncio.gather(*(self.close_session(sid) for sid in tuple(self.sessions)))


@pytest_asyncio.fixture
async def fixture(monkeypatch):
    layer, services, slots, reads = Layer(), [], set(), []
    current = {"revision": 1, "denied": False, "admission": True}

    async def build(**values):
        reads.append(values)
        if current["denied"]:
            raise RuntimeError("credential-bearing fixture error")
        if values.get("delegation_enabled"):
            values["delegation_targets"] = ("repo", "qa")
        return SimpleNamespace(revision=current["revision"], **values)

    async def acquire(sid, **values):
        assert values["target"] == "local" and values["execution_path"] == "copilot-cli"
        if current["admission"]:
            slots.add(sid)
        return current["admission"]

    async def history(user, agent):
        if current["denied"]:
            raise RuntimeError("denied")

    monkeypatch.setattr(module, "authorize_copilot_history", history)
    monkeypatch.setattr(module, "build_copilot_agent_config", build)
    monkeypatch.setattr(module, "acquire_chat_slot", acquire)
    monkeypatch.setattr(module, "release_chat_slot", slots.discard)

    def service(**options):
        options.setdefault("watch_interval", 0.01)
        options.setdefault("store", MemoryConversations())
        instance = CopilotChatService(layer, **options)
        services.append(instance)
        return instance

    result = SimpleNamespace(
        layer=layer, service=service, slots=slots, reads=reads, current=current,
        user=UserContext("human-one", "one@example.invalid", "One", "member"),
        other=UserContext("human-two", "two@example.invalid", "Two", "admin"),
    )
    yield result
    layer.close_hook = None
    for instance in services:
        try:
            await instance.aclose()
        except CopilotChatError:
            for entry in tuple(instance._entries.values()):
                await layer.close_session(entry.sid)
                slots.discard(entry.sid)
    assert not slots


async def create(fixture, service, user=None):
    return await service.create(user or fixture.user, "agent", "account", "model")


async def gone(fixture, service, sid):
    async with asyncio.timeout(1):
        while sid in service._entries:
            await asyncio.sleep(0.001)
    assert sid not in fixture.layer.sessions and sid not in fixture.slots


@pytest.mark.asyncio
async def test_successful_turn_is_flat_done_only_after_exhaustion_and_keeps_session_warm(fixture):
    service = fixture.service()
    sid = await create(fixture, service)
    turn = await service.prepare_turn(fixture.user, sid, "hello")
    frames = [frame async for frame in turn]
    await turn.aclose()
    assert frames == [{"type": "text", "content": "fixture response"}, {"type": "turn_complete"}]
    assert sid in fixture.layer.sessions and sid in fixture.slots
    assert service._entries[sid].turn is None
    assert fixture.reads[0]["account_scope"].user_sub == fixture.user.sub
    assert fixture.reads[0]["enabled_tools"] == module.SUPPORTED_NATIVE_TOOLS
    assert fixture.reads[0]["client_type"] == "dashboard"
    next_turn = await service.prepare_turn(fixture.user, sid, "again")
    assert [frame async for frame in next_turn][-1] == {"type": "turn_complete"}
    assert len(fixture.layer.messages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{"is_api_key": True}, {"session_id": "agent-session"}, {"agent": "agent"},
                                   {"external_claim": "phone:caller"}])
async def test_nonhuman_principals_cannot_create(fixture, fields):
    service = fixture.service()
    with pytest.raises(CopilotChatError) as caught:
        await create(fixture, service, replace(fixture.user, **fields))
    assert caught.value.status_code == 403
    assert not fixture.reads and not fixture.slots and not fixture.layer.started


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prepare", "close", "permission", "question"])
async def test_other_human_including_admin_cannot_drive_original_owner(fixture, operation):
    service = fixture.service()
    sid = await create(fixture, service)
    calls = {
        "prepare": lambda: service.prepare_turn(fixture.other, sid, "steal"),
        "close": lambda: service.close(fixture.other, sid),
        "permission": lambda: service.permission(fixture.other, sid, "request", True),
        "question": lambda: service.question(fixture.other, sid, "request", {"q": {"answers": ["yes"]}}),
    }
    with pytest.raises(CopilotChatError) as caught:
        await calls[operation]()
    assert caught.value.status_code == 404
    assert sid in fixture.slots and sid in fixture.layer.sessions


@pytest.mark.asyncio
async def test_limits_include_startup_and_reject_before_another_runtime(fixture):
    service = fixture.service(max_sessions=2, max_per_user=1)
    started, finish = asyncio.Event(), asyncio.Event()

    async def hold(_sid):
        started.set()
        await finish.wait()

    fixture.layer.start_hook = hold
    first = asyncio.create_task(create(fixture, service))
    await started.wait()
    with pytest.raises(CopilotChatError) as caught:
        await create(fixture, service)
    assert caught.value.status_code == 429
    finish.set()
    await first
    await create(fixture, service, fixture.other)
    third = replace(fixture.user, sub="third-human")
    with pytest.raises(CopilotChatError) as caught:
        await create(fixture, service, third)
    assert caught.value.status_code == 429 and len(fixture.layer.started) == 2


@pytest.mark.asyncio
async def test_shared_slot_denial_releases_service_reservation(fixture):
    service = fixture.service()
    fixture.current["admission"] = False
    with pytest.raises(CopilotChatError) as caught:
        await create(fixture, service)
    assert caught.value.status_code == 429
    assert not service._entries and not fixture.slots and not fixture.layer.started


@pytest.mark.asyncio
async def test_cancelled_startup_joins_owned_layer_and_releases_slot(fixture):
    service = fixture.service()
    started = asyncio.Event()

    async def hold(_sid):
        started.set()
        await asyncio.Event().wait()

    fixture.layer.start_hook = hold
    task = asyncio.create_task(create(fixture, service))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service._entries and not fixture.slots and not fixture.layer.sessions


@pytest.mark.asyncio
async def test_startup_that_swallows_cancellation_cannot_return_live_session(fixture):
    service = fixture.service()
    started = asyncio.Event()

    async def swallow(_sid):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    fixture.layer.start_hook = swallow
    task = asyncio.create_task(create(fixture, service))
    await started.wait()
    task.cancel()
    with pytest.raises((asyncio.CancelledError, CopilotChatError)):
        await task
    assert not service._entries and not fixture.slots and not fixture.layer.sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", " ", "x" * 65537, "\ud800", None])
async def test_invalid_text_is_rejected_before_turn_creation(fixture, text):
    service = fixture.service()
    sid = await create(fixture, service)
    with pytest.raises(CopilotChatError) as caught:
        await service.prepare_turn(fixture.user, sid, text)
    assert caught.value.status_code == 422
    assert service._entries[sid].turn is None and not fixture.layer.messages


@pytest.mark.asyncio
async def test_busy_is_rejected_before_second_producer_and_unstarted_close_joins(fixture):
    service = fixture.service()
    sid = await create(fixture, service)
    turn = await service.prepare_turn(fixture.user, sid, "first")
    with pytest.raises(CopilotChatError) as caught:
        await service.prepare_turn(fixture.user, sid, "second")
    assert caught.value.status_code == 409
    await turn.aclose()  # Never called __anext__: must still close ownership.
    await gone(fixture, service, sid)
    assert all(text != "second" for _, text in fixture.layer.messages)


@pytest.mark.asyncio
async def test_unconsumed_prepared_turn_deadline_closes_even_after_native_done(fixture):
    service = fixture.service(turn_timeout=0.04)
    sid = await create(fixture, service)
    turn = await service.prepare_turn(fixture.user, sid, "abandoned")
    await gone(fixture, service, sid)
    assert not turn._complete
    await turn.aclose()


@pytest.mark.asyncio
async def test_cancelled_consumer_joins_runtime_before_cancellation_returns(fixture):
    service = fixture.service()
    producing, closing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def program(_sid):
        producing.set()
        await asyncio.Event().wait()
        yield CommonEvent("done", {})

    async def close(_sid):
        closing.set()
        await finish.wait()

    fixture.layer.program, fixture.layer.close_hook = program, close
    sid = await create(fixture, service)
    turn = await service.prepare_turn(fixture.user, sid, "disconnect")
    consumer = asyncio.create_task(anext(turn))
    await producing.wait()
    consumer.cancel()
    await closing.wait()
    consumer.cancel()
    await asyncio.sleep(0)
    assert not consumer.done() and sid in fixture.slots
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await gone(fixture, service, sid)


@pytest.mark.asyncio
async def test_native_done_without_producer_exhaustion_never_reports_complete(fixture):
    service = fixture.service(turn_timeout=0.04)

    async def program(_sid):
        yield CommonEvent("done", {})
        await asyncio.Event().wait()

    fixture.layer.program = program
    sid = await create(fixture, service)
    turn = await service.prepare_turn(fixture.user, sid, "wait")
    frames = [frame async for frame in turn]
    assert frames == [{"type": "error", "message": "Copilot turn did not complete"}]
    await turn.aclose()
    await gone(fixture, service, sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "event", "missing-done", "duplicate-done", "oversized-output"])
async def test_producer_failures_close_and_never_echo_raw_error(fixture, failure):
    service = fixture.service()

    async def program(_sid):
        if failure == "exception":
            raise RuntimeError("credential-bearing fixture error")
        if failure == "event":
            yield CommonEvent("error", {"message": "credential-bearing fixture error"})
        elif failure == "duplicate-done":
            yield CommonEvent("done", {})
            yield CommonEvent("done", {})
        elif failure == "oversized-output":
            yield CommonEvent("text", {"content": "x" * (1024 * 1024 + 1)})
        else:
            yield CommonEvent("text", {"content": "partial"})

    fixture.layer.program = program
    sid = await create(fixture, service)
    turn = await service.prepare_turn(fixture.user, sid, "fail")
    frames = [frame async for frame in turn]
    await turn.aclose()
    assert not any(frame["type"] == "turn_complete" for frame in frames)
    assert "credential-bearing" not in str(frames)
    await gone(fixture, service, sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission_prompt", "question_prompt"])
async def test_real_platform_waiters_are_multiplexed_and_exact_kind_scoped(fixture, kind):
    service = fixture.service()
    request_id = str(uuid.uuid4())
    results = []
    tool_input = {"fixture": True} if kind == "permission_prompt" else {"questions": [{
        "id": "question", "options": [{"label": "chosen"}], "multiSelect": False, "isOther": True,
    }]}

    async def program(sid):
        await state.get_permission_queue(sid).put({"event_type": kind, "request_id": request_id,
                                                   "tool_name": "fixture", "tool_input": tool_input})
        if kind == "permission_prompt":
            results.append(await state.wait_for_permission(request_id, sid, timeout=1))
        else:
            results.append(await state.wait_for_question(request_id, sid, timeout=1))
        yield CommonEvent("done", {})

    fixture.layer.program = program
    sid = await create(fixture, service)
    turn = await service.prepare_turn(fixture.user, sid, "approval")
    prompt = await asyncio.wait_for(anext(turn), 1)
    assert prompt == {"type": kind, "request_id": request_id, "tool_name": "fixture", "tool_input": tool_input}
    answers = {"question": {"answers": ["chosen", "free text"]}}
    if kind == "permission_prompt":
        with pytest.raises(CopilotChatError):
            await service.question(fixture.user, sid, request_id, answers)
        await service.permission(fixture.user, sid, request_id, False)
        assert results == []  # Native waiter resumes only after control returns.
    else:
        with pytest.raises(CopilotChatError):
            await service.permission(fixture.user, sid, request_id, True)
        with pytest.raises(CopilotChatError) as caught:
            await service.question(fixture.user, sid, request_id, {"different-question": {"answers": ["chosen"]}})
        assert caught.value.status_code == 422
        assert not state._question_events[request_id].is_set()
        await service.question(fixture.user, sid, request_id, answers)
    assert [frame async for frame in turn] == [{"type": "turn_complete"}]
    assert results == ([False] if kind == "permission_prompt" else [answers])
    with pytest.raises(CopilotChatError):
        await service.permission(fixture.user, sid, request_id, True)


@pytest.mark.asyncio
async def test_access_revocation_closes_held_permission_and_denies_waiter(fixture):
    service = fixture.service()
    request_id, results = str(uuid.uuid4()), []

    async def program(sid):
        waiter = asyncio.create_task(state.wait_for_permission(request_id, sid, timeout=1))
        await asyncio.sleep(0)  # Match the real hook's registered-before-yield order.
        await state.get_permission_queue(sid).put({"event_type": "permission_prompt", "request_id": request_id})
        try:
            results.append(await asyncio.shield(waiter))
        except asyncio.CancelledError:
            results.append(await waiter)
            raise
        yield CommonEvent("done", {})

    fixture.layer.program = program
    sid = await create(fixture, service)
    turn = await service.prepare_turn(fixture.user, sid, "held")
    assert (await asyncio.wait_for(anext(turn), 1))["type"] == "permission_prompt"
    fixture.current["denied"] = True
    await gone(fixture, service, sid)
    assert results == [False]
    await turn.aclose()


@pytest.mark.asyncio
async def test_original_owner_can_close_after_access_revocation_without_database_read(fixture):
    service = fixture.service(watch_interval=10)
    sid = await create(fixture, service)
    fixture.current["denied"] = True
    before = len(fixture.reads)
    await service.close(fixture.user, sid)
    assert len(fixture.reads) == before
    await gone(fixture, service, sid)


@pytest.mark.asyncio
async def test_profile_change_revalidated_before_next_turn(fixture):
    service = fixture.service(watch_interval=10)
    sid = await create(fixture, service)
    fixture.current["revision"] = 2
    with pytest.raises(CopilotChatError) as caught:
        await service.prepare_turn(fixture.user, sid, "must not submit")
    assert caught.value.status_code == 403 and caught.value.__context__ is None
    assert not fixture.layer.messages
    await gone(fixture, service, sid)


@pytest.mark.asyncio
async def test_idle_cleanup_skips_busy_turn_but_closes_idle_session(fixture):
    service = fixture.service(idle_timeout=0.025, turn_timeout=1)

    async def program(_sid):
        yield CommonEvent("text", {"content": "waiting"})
        await asyncio.Event().wait()

    fixture.layer.program = program
    busy = await create(fixture, service)
    idle = await create(fixture, service)
    turn = await service.prepare_turn(fixture.user, busy, "busy")
    await anext(turn)
    await gone(fixture, service, idle)
    assert busy in service._entries and busy in fixture.layer.sessions
    await turn.aclose()


@pytest.mark.asyncio
async def test_runtime_death_releases_service_entry_and_shared_slot(fixture):
    service = fixture.service()
    sid = await create(fixture, service)
    fixture.layer.sessions.pop(sid)
    await gone(fixture, service, sid)


@pytest.mark.asyncio
async def test_failed_cleanup_retains_capacity_and_shared_reservation(fixture):
    service = fixture.service(max_sessions=1, max_per_user=1)
    sid = await create(fixture, service)

    async def fail(_sid):
        raise RuntimeError("credential-bearing fixture error")

    fixture.layer.close_hook = fail
    with pytest.raises(CopilotChatError) as caught:
        await service.close(fixture.user, sid)
    assert caught.value.status_code == 503 and caught.value.__context__ is None
    assert sid in service._entries and sid in fixture.slots
    with pytest.raises(CopilotChatError) as caught:
        await create(fixture, service)
    assert caught.value.status_code == 429


@pytest.mark.asyncio
async def test_shutdown_seals_before_await_and_repeated_cancel_joins_all_owners(fixture):
    service = fixture.service()
    sid = await create(fixture, service)
    started, finish = asyncio.Event(), asyncio.Event()

    async def hold(_sid):
        started.set()
        await finish.wait()

    fixture.layer.close_hook = hold
    task = asyncio.create_task(service.aclose())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    with pytest.raises(CopilotChatError) as caught:
        await create(fixture, service)
    assert caught.value.status_code == 503
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sid not in fixture.slots and not fixture.layer.sessions


@pytest.mark.asyncio
async def test_cleanup_preserves_replacement_security_context_reservation(fixture, monkeypatch):
    from auth.path_policy import SecurityContext

    service = fixture.service()
    sid = await create(fixture, service)
    replacement = SecurityContext(role="viewer", username="other", agent="other", is_admin_agent=False)
    monkeypatch.setitem(state._session_security, sid, replacement)
    try:
        await service.close(fixture.user, sid)
        assert sid not in fixture.layer.sessions and sid not in service._entries
        assert state.get_session_security(sid) is replacement
        assert sid in fixture.slots
    finally:
        fixture.slots.discard(sid)  # The injected replacement owns this slot.


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["context", "owned", "legacy"])
async def test_failed_start_cannot_release_visible_foreign_registration(fixture, monkeypatch, foreign):
    from auth.path_policy import SecurityContext
    from core.session import owned_sessions, session_manager

    service = fixture.service()
    foreign_ids, handles = set(), []
    original = session_manager.has_legacy_session
    monkeypatch.setattr(session_manager, "has_legacy_session",
                        lambda sid: sid in foreign_ids or original(sid))

    async def no_close():
        pytest.fail("Foreign owner must never be closed")

    async def refused(sid, _config, **_kwargs):
        if foreign == "legacy":
            foreign_ids.add(sid)
        if foreign == "context":
            monkeypatch.setitem(state._session_security, sid,
                                SecurityContext(role="viewer", username="other", agent="other", is_admin_agent=False))
        elif foreign == "owned":
            handles.append(owned_sessions.register_owned_session(
                session_id=sid, engine="other", agent="other", user_sub="other", username="other",
                active=lambda: False, close=no_close,
            ))
        fixture.current["foreign_sid"] = sid
        raise RuntimeError("A foreign owner claimed the session before registration")

    monkeypatch.setattr(fixture.layer, "start_session", refused)
    try:
        with pytest.raises(CopilotChatError):
            await create(fixture, service)
        sid = fixture.current["foreign_sid"]
        assert sid in fixture.slots
        assert sid not in service._entries and sid not in fixture.layer.sessions
        if handles:
            assert owned_sessions.get_owned_session(sid) is handles[0]
    finally:
        for handle in handles:
            owned_sessions.release_owned_session(handle)
        fixture.slots.discard(fixture.current.get("foreign_sid"))


@pytest.mark.asyncio
async def test_denied_admission_does_not_release_a_slot_it_never_acquired(fixture, monkeypatch):
    service = fixture.service()
    foreign = set()

    async def denied(sid, **_options):
        # An unrelated reservation exists, but this admission did not succeed.
        fixture.slots.add(sid)
        foreign.add(sid)
        return False

    monkeypatch.setattr(module, "acquire_chat_slot", denied)
    try:
        with pytest.raises(CopilotChatError) as error:
            await create(fixture, service)
        assert error.value.status_code == 429
        assert foreign and foreign <= fixture.slots
    finally:
        fixture.slots.difference_update(foreign)


@pytest.mark.asyncio
async def test_cancellation_swallowed_by_admission_records_then_releases_safe_slot(fixture, monkeypatch):
    service = fixture.service()
    entered = asyncio.Event()

    async def late(sid, **_options):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            fixture.slots.add(sid)
            return True

    monkeypatch.setattr(module, "acquire_chat_slot", late)
    pending = asyncio.create_task(create(fixture, service))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    with pytest.raises((asyncio.CancelledError, CopilotChatError)):
        await asyncio.wait_for(pending, 1)
    assert not fixture.slots and not fixture.layer.started and not service._entries


@pytest.mark.asyncio
async def test_durable_order_precedes_native_dispatch_and_every_delivered_frame(fixture):
    service = fixture.service()
    handle = await create(fixture, service)
    cid = service.conversation_id(fixture.user, handle)

    async def program(sid):
        assert service.store.events(cid, fixture.user.sub)[0]["type"] == "user"
        yield CommonEvent("thinking", {"content": "private reasoning"})
        yield CommonEvent("text", {"content": "durable"})
        yield CommonEvent("done", {})

    fixture.layer.program = program
    turn = await service.prepare_turn(fixture.user, handle, "persist first")
    async for frame in turn:
        assert any({key: value for key, value in event.items() if key != "seq"} == frame
                   for event in service.store.events(cid, fixture.user.sub))
    await service.close(fixture.user, handle)
    history = await service.get_conversation(fixture.user, cid)
    assert [event["type"] for event in history["events"]] == ["user", "text", "turn_complete"]
    assert history["conversation"]["can_resume"]
    assert not {"platform_session_id", "generation", "user_sub"} & history["conversation"].keys()


@pytest.mark.asyncio
async def test_cold_resume_preserves_config_rotates_handle_and_rejects_stale_driver(fixture):
    store = MemoryConversations()
    first = fixture.service(store=store)
    old = await create(fixture, first)
    cid = first.conversation_id(fixture.user, old)
    assert [frame async for frame in await first.prepare_turn(fixture.user, old, "remember")][-1]["type"] == "turn_complete"
    await first.close(fixture.user, old)
    second = fixture.service(store=store)
    saved = await second.get_conversation(fixture.user, cid)
    handle = await second.resume(fixture.user, cid, saved["conversation"]["revision"])
    assert handle != old and second.conversation_id(fixture.user, handle) == cid
    sid, config = fixture.layer.started[-1]
    assert sid == old and config.resume is True
    assert (config.agent_name, config.account_id, config.model, config.permission_mode) == ("agent", "account", "model", "default")
    with pytest.raises(CopilotChatError) as caught:
        await second.close(fixture.user, old)
    assert caught.value.status_code == 404 and sid in fixture.layer.sessions
    assert [frame async for frame in await second.prepare_turn(fixture.user, handle, "recall")][-1]["type"] == "turn_complete"


@pytest.mark.asyncio
async def test_wrong_owner_and_revoked_agent_cannot_read_or_resume(fixture):
    service = fixture.service()
    handle = await create(fixture, service)
    cid = service.conversation_id(fixture.user, handle)
    for operation in (service.get_conversation(fixture.other, cid), service.resume(fixture.other, cid, 1)):
        with pytest.raises(CopilotChatError) as caught:
            await operation
        assert caught.value.status_code == 404
    assert (await service.list_conversations(fixture.other))["conversations"] == []
    fixture.current["denied"] = True
    with pytest.raises(CopilotChatError) as caught:
        await service.get_conversation(fixture.user, cid)
    assert caught.value.status_code == 403
    assert (await service.list_conversations(fixture.user))["conversations"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["begin_turn", "append_event", "finish_turn"])
async def test_failed_durable_write_never_fabricates_completion(fixture, operation):
    service = fixture.service()
    handle = await create(fixture, service)
    cid = service.conversation_id(fixture.user, handle)

    def failure(*args, **kwargs):
        raise RuntimeError("private SQL credential text")

    setattr(service.store, operation, failure)
    if operation == "begin_turn":
        with pytest.raises(CopilotChatError) as caught:
            await service.prepare_turn(fixture.user, handle, "failure")
        assert "private" not in str(caught.value) and not fixture.layer.messages
    else:
        frames = [frame async for frame in await service.prepare_turn(fixture.user, handle, "failure")]
        assert not any(frame["type"] == "turn_complete" for frame in frames)
    await gone(fixture, service, handle)
    assert service.store.get(cid, fixture.user.sub)["state"] == "incomplete"


@pytest.mark.asyncio
async def test_partial_turn_is_quarantined_and_resume_never_starts_runtime(fixture):
    service = fixture.service()
    handle = await create(fixture, service)
    cid = service.conversation_id(fixture.user, handle)

    async def program(sid):
        yield CommonEvent("text", {"content": "partial"})
        await asyncio.Event().wait()

    fixture.layer.program = program
    turn = await service.prepare_turn(fixture.user, handle, "partial")
    assert (await anext(turn))["type"] == "text"
    await turn.aclose()
    row = service.store.get(cid, fixture.user.sub)
    assert row["state"] == "incomplete"
    with pytest.raises(CopilotChatError) as caught:
        await service.resume(fixture.user, cid, row["revision"])
    assert caught.value.status_code == 409 and len(fixture.layer.started) == 1


@pytest.mark.asyncio
async def test_concurrent_resume_claim_has_one_owner_and_loser_cannot_quarantine(fixture):
    service = fixture.service()
    old = await create(fixture, service)
    cid = service.conversation_id(fixture.user, old)
    _ = [frame async for frame in await service.prepare_turn(fixture.user, old, "ready")]
    await service.close(fixture.user, old)
    revision = service.store.get(cid, fixture.user.sub)["revision"]
    results = await asyncio.gather(service.resume(fixture.user, cid, revision),
                                   service.resume(fixture.user, cid, revision), return_exceptions=True)
    assert sum(isinstance(value, str) for value in results) == 1
    row = service.store.get(cid, fixture.user.sub)
    assert row["state"] == "open" and row["generation"] in results


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "begin_turn", "finish_turn"])
async def test_cancelled_database_mutation_joins_before_cleanup(fixture, operation):
    import threading

    service = fixture.service()
    entered, release = threading.Event(), threading.Event()
    original = getattr(service.store, operation)

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    setattr(service.store, operation, held)
    if operation == "create":
        task = asyncio.create_task(create(fixture, service))
    else:
        handle = await create(fixture, service)
        if operation == "begin_turn":
            task = asyncio.create_task(service.prepare_turn(fixture.user, handle, "cancel"))
        else:
            turn = await service.prepare_turn(fixture.user, handle, "cancel")
            task = asyncio.create_task(anext(turn))
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        entry = next(iter(service._entries.values()))
        if operation == "finish_turn":
            task = asyncio.create_task(service.close(fixture.user, entry.handle))
        else:
            task.cancel()
        await asyncio.sleep(0.01)
        assert entry.sid in service._entries and not task.done()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await gone(fixture, service, entry.sid)
        assert not entry.mutations
        row = service.store.get(entry.cid, fixture.user.sub)
        assert row["state"] == "incomplete"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_store_close_keeps_capacity_claim_after_runtime_join(fixture):
    service = fixture.service(max_sessions=1, max_per_user=1)
    handle = await create(fixture, service)
    service.store.finish_close = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private"))
    with pytest.raises(CopilotChatError) as caught:
        await service.close(fixture.user, handle)
    assert caught.value.status_code == 503
    assert handle in service._entries and handle not in fixture.layer.sessions
    with pytest.raises(CopilotChatError) as caught:
        await create(fixture, service)
    assert caught.value.status_code == 429


@pytest.mark.asyncio
async def test_losing_resume_in_another_service_never_closes_shared_layer_winner(fixture):
    store = MemoryConversations()
    first = fixture.service(store=store)
    sid = await create(fixture, first)
    cid = first.conversation_id(fixture.user, sid)
    _ = [frame async for frame in await first.prepare_turn(fixture.user, sid, "ready")]
    await first.close(fixture.user, sid)
    revision = store.get(cid, fixture.user.sub)["revision"]
    winner = await first.resume(fixture.user, cid, revision)
    closes = len(fixture.layer.closed)
    loser = fixture.service(store=store)
    with pytest.raises(CopilotChatError) as caught:
        await loser.resume(fixture.user, cid, revision)
    assert caught.value.status_code == 409
    assert len(fixture.layer.closed) == closes and sid in fixture.layer.sessions
    assert store.get(cid, fixture.user.sub)["generation"] == winner
    assert not loser._entries


@pytest.mark.asyncio
async def test_stalled_read_times_out_without_waiting_or_creating_owner(fixture):
    import threading

    service = fixture.service(authorization_timeout=0.02)
    entered, release = threading.Event(), threading.Event()

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return []

    service.store.list_conversations = held
    try:
        async with asyncio.timeout(0.2):
            with pytest.raises(CopilotChatError) as caught:
                await service.list_conversations(fixture.user)
        assert caught.value.status_code == 503 and entered.is_set()
        assert not service._entries and not fixture.layer.started
    finally:
        release.set()


@pytest.mark.asyncio
async def test_stalled_mutation_retains_generation_and_capacity_after_bounded_cleanup(fixture):
    import threading

    service = fixture.service(database_timeout=0.02, max_sessions=1, max_per_user=1)
    entered, release = threading.Event(), threading.Event()
    original = service.store.create

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    service.store.create = held
    try:
        async with asyncio.timeout(0.3):
            with pytest.raises(CopilotChatError) as caught:
                await create(fixture, service)
        assert caught.value.status_code == 503 and entered.is_set()
        entry = next(iter(service._entries.values()))
        assert entry.mutations and entry.closing.done() and not fixture.layer.started
        with pytest.raises(CopilotChatError) as caught:
            await create(fixture, service)
        assert caught.value.status_code == 429
        release.set()
        async with asyncio.timeout(1):
            while entry.mutations:
                await asyncio.sleep(0.001)
        row = service.store.get(entry.cid, fixture.user.sub)
        assert row["generation"] == entry.handle and row["state"] == "open"
        assert service._entries[entry.sid] is entry  # Late commit is never reclaimed or resumed.
    finally:
        release.set()


@pytest.mark.asyncio
async def test_read_history_survives_removed_account_but_fresh_resume_is_denied(fixture, monkeypatch):
    service = fixture.service()
    handle = await create(fixture, service)
    cid = service.conversation_id(fixture.user, handle)
    _ = [frame async for frame in await service.prepare_turn(fixture.user, handle, "saved")]
    await service.close(fixture.user, handle)
    async def removed(**kwargs):
        raise RuntimeError("account removed")
    monkeypatch.setattr(module, "build_copilot_agent_config", removed)
    saved = await service.get_conversation(fixture.user, cid)
    assert saved["events"] and saved["conversation"]["can_resume"]
    with pytest.raises(CopilotChatError):
        await service.resume(fixture.user, cid, saved["conversation"]["revision"])
    assert len(fixture.layer.started) == 1
    assert service.store.get(cid, fixture.user.sub)["state"] == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize('detached', [False, True])
async def test_failed_terminal_delivery_quarantines_even_after_iterator_detaches(fixture, detached):
    service = fixture.service()
    handle = await create(fixture, service)
    cid = service.conversation_id(fixture.user, handle)
    turn = await service.prepare_turn(fixture.user, handle, 'complete native work')
    while (await anext(turn))['type'] != 'turn_complete':
        pass
    if detached:
        with pytest.raises(StopAsyncIteration):
            await anext(turn)
        assert service._entries[handle].turn is None
    turn.delivery_failed()
    await service.close(fixture.user, handle)
    saved = await service.get_conversation(fixture.user, cid)
    assert saved['events'][-1]['type'] == 'turn_complete'
    assert saved['conversation']['state'] == 'incomplete'
    assert saved['conversation']['can_resume'] is False


@pytest.mark.asyncio
async def test_agent_page_filters_before_pagination_and_checks_empty_page_authority(fixture):
    service = fixture.service()
    handle = await create(fixture, service)
    cid = service.conversation_id(fixture.user, handle)
    store = service.store
    store.create(str(uuid.uuid4()), fixture.user.sub, agent='another-agent', account_id='account', model='model',
                 permission_mode='default', platform_session_id=str(uuid.uuid4()), generation=str(uuid.uuid4()))
    page = await service.list_conversations(fixture.user, limit=1, agent='agent')
    assert [item['id'] for item in page['conversations']] == [cid]
    assert not page['has_more']
    fixture.current['denied'] = True
    with pytest.raises(CopilotChatError) as error:
        await service.list_conversations(fixture.user, agent='no-history')
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_agent_page_mismatch_cannot_read_or_resume_or_mutate_saved_generation(fixture):
    service = fixture.service()
    handle = await create(fixture, service)
    cid = service.conversation_id(fixture.user, handle)
    assert [event async for event in await service.prepare_turn(fixture.user, handle, 'complete')][-1]['type'] == 'turn_complete'
    await service.close(fixture.user, handle)
    before = service.store.get(cid, fixture.user.sub)
    starts = len(fixture.layer.started)
    for operation in [service.get_conversation(fixture.user, cid, agent='another-agent'),
                      service.resume(fixture.user, cid, before['revision'], agent='another-agent')]:
        with pytest.raises(CopilotChatError) as error:
            await operation
        assert error.value.status_code == 404
    assert service.store.get(cid, fixture.user.sub) == before
    assert len(fixture.layer.started) == starts
    assert (await service.get_conversation(fixture.user, cid, agent='agent'))['conversation']['id'] == cid


@pytest_asyncio.fixture
async def catalog_fixture(fixture, monkeypatch):
    fixture.current['credential'] = 'generation-one'
    fixture.catalogs = []
    fixture.catalog_hook = None
    fixture.models = [{'id': 'model-a', 'name': 'Model A', 'available': True,
                       'policy': 'enabled', 'multiplier': 1.0}]

    async def credential(self, entry):
        assert entry.user.sub in {fixture.user.sub, fixture.other.sub}
        return fixture.current['credential']

    async def models(sid, config):
        fixture.catalogs.append((sid, config))
        fixture.layer.sessions[sid] = asyncio.Lock()
        try:
            if fixture.catalog_hook:
                await fixture.catalog_hook()
            return fixture.models
        finally:
            await fixture.layer.close_session(sid)

    monkeypatch.setattr(CopilotChatService, '_model_credential', credential)
    monkeypatch.setattr(fixture.layer, 'list_models', models, raising=False)
    return fixture


@pytest.mark.asyncio
async def test_model_catalog_is_personal_bounded_and_leaves_no_conversation_or_owner(catalog_fixture):
    f = catalog_fixture
    service = f.service()
    assert await service.list_models(f.user, 'agent', 'account-a') == {'models': f.models}
    assert not service._entries and not service.store.rows and not f.slots
    assert not f.layer.started and not f.layer.messages and not f.layer.sessions
    assert f.reads[0]['account_id'] == 'account-a'
    assert f.reads[0]['account_scope'].user_sub == f.user.sub
    assert len(f.reads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['revision', 'credential', 'denied'])
async def test_model_catalog_rechecks_agent_and_exact_account_after_discovery(catalog_fixture, field):
    f = catalog_fixture
    service = f.service()

    async def change():
        f.current[field] = {'revision': 2, 'credential': 'generation-two', 'denied': True}[field]
    f.catalog_hook = change
    with pytest.raises(CopilotChatError):
        await service.list_models(f.user, 'agent', 'account-a')
    assert not service._entries and not service.store.rows and not f.slots and not f.layer.sessions


@pytest.mark.asyncio
async def test_model_discovery_reserves_capacity_before_await_and_shares_chat_limits(catalog_fixture):
    f = catalog_fixture
    service = f.service(max_sessions=1, max_per_user=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()
    f.catalog_hook = hold
    pending = asyncio.create_task(service.list_models(f.user, 'agent', 'account-a'))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for operation in (service.list_models(f.other, 'agent', 'account-b'), create(f, service)):
            with pytest.raises(CopilotChatError) as caught:
                await operation
            assert caught.value.status_code == 429
        assert len(f.catalogs) == 1 and len(f.slots) == 1
    finally:
        release.set()
        await pending
    assert not f.slots


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['cancel', 'timeout', 'shutdown'])
async def test_model_discovery_cancellation_and_shutdown_join_runtime_cleanup(catalog_fixture, action):
    f = catalog_fixture
    service = f.service(model_timeout=0.05 if action == 'timeout' else 45)
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await asyncio.Event().wait()

    async def close(sid):
        cleanup.set()
        await release.wait()
    f.catalog_hook = hold
    f.layer.close_hook = close
    pending = asyncio.create_task(service.list_models(f.user, 'agent', 'account-a'))
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if action == 'cancel':
            pending.cancel()
        elif action == 'shutdown':
            shutdown = asyncio.create_task(service.aclose())
        await asyncio.wait_for(cleanup.wait(), 1)
        assert not pending.done() and f.slots and service._entries
        release.set()
        results = await asyncio.gather(pending, return_exceptions=True)
        assert isinstance(results[0], (asyncio.CancelledError, CopilotChatError))
        if shutdown:
            await shutdown
        assert not f.slots and not service._entries and not f.layer.sessions and not service.store.rows
    finally:
        release.set()
        await asyncio.gather(pending, *([shutdown] if shutdown else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_model_cleanup_retains_capacity_and_never_returns_inventory(catalog_fixture):
    f = catalog_fixture
    service = f.service(max_sessions=1, max_per_user=1)

    async def failed(sid):
        raise RuntimeError('private cleanup details')
    f.layer.close_hook = failed
    with pytest.raises(CopilotChatError) as caught:
        await service.list_models(f.user, 'agent', 'account-a')
    assert 'private' not in str(caught.value)
    assert service._entries and f.slots and not service.store.rows
    with pytest.raises(CopilotChatError) as caught:
        await service.list_models(f.other, 'agent', 'account-b')
    assert caught.value.status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize('fields', [{'is_api_key': True}, {'session_id': 'session'}, {'agent': 'agent'}, {'external_claim': 'phone'}])
async def test_model_discovery_rejects_nonhuman_before_authority_or_runtime(catalog_fixture, fields):
    f = catalog_fixture
    service = f.service()
    with pytest.raises(CopilotChatError) as caught:
        await service.list_models(replace(f.user, **fields), 'agent', 'account-a')
    assert caught.value.status_code == 403
    assert not f.reads and not f.catalogs and not f.slots


@pytest.mark.asyncio
async def test_model_discovery_denied_admission_never_starts_runtime(catalog_fixture):
    f = catalog_fixture
    f.current['admission'] = False
    service = f.service()
    with pytest.raises(CopilotChatError) as caught:
        await service.list_models(f.user, 'agent', 'account-a')
    assert caught.value.status_code == 429
    assert not f.catalogs and not service._entries and not service.store.rows


@pytest.mark.asyncio
async def test_internal_model_cancellation_is_unavailable_not_http_caller_cancellation(catalog_fixture):
    f = catalog_fixture
    service = f.service()

    async def revoked():
        raise asyncio.CancelledError
    f.catalog_hook = revoked
    with pytest.raises(CopilotChatError) as caught:
        await service.list_models(f.user, 'agent', 'account-a')
    assert caught.value.status_code == 503
    assert not service._entries and not f.slots and not f.layer.sessions


@pytest.mark.asyncio
@pytest.mark.parametrize('effort', [None, 'low', 'medium', 'high', 'xhigh', 'max'])
async def test_reasoning_selection_is_saved_and_cold_resume_uses_the_original(fixture, effort):
    service = fixture.service()
    handle = await service.create(fixture.user, 'agent', 'account', 'model', reasoning_effort=effort)
    cid = service.conversation_id(fixture.user, handle)
    config = fixture.layer.started[-1][1]
    assert getattr(config, 'reasoning_effort', None) == effort
    turn = await service.prepare_turn(fixture.user, handle, 'Remember this selection')
    assert [event async for event in turn][-1] == {'type': 'turn_complete'}
    await service.close(fixture.user, handle)
    row = (await service.get_conversation(fixture.user, cid))['conversation']
    assert row['reasoning_effort'] == effort
    assert (await service.list_conversations(fixture.user))['conversations'][0]['reasoning_effort'] == effort
    fresh = fixture.service(store=service.store)
    resumed = await fresh.resume(fixture.user, cid, row['revision'])
    assert resumed != handle
    assert getattr(fixture.layer.started[-1][1], 'reasoning_effort', None) == effort
    assert service.store.get(cid, fixture.user.sub)['reasoning_effort'] == effort


@pytest.mark.asyncio
@pytest.mark.parametrize('effort', ['', 'auto', 'minimal', 'HIGH', ' high', True, 1, [], {}])
async def test_invalid_reasoning_is_rejected_before_storage_capacity_or_runtime(fixture, effort):
    service = fixture.service()
    with pytest.raises(CopilotChatError) as caught:
        await service.create(fixture.user, 'agent', 'account', 'model', reasoning_effort=effort)
    assert caught.value.status_code == 422
    assert not fixture.reads and not fixture.slots and not fixture.layer.started and not service.store.rows


@pytest.mark.asyncio
async def test_legacy_history_without_effort_keeps_model_default_on_resume(fixture):
    service = fixture.service()
    handle = await create(fixture, service)
    cid = service.conversation_id(fixture.user, handle)
    turn = await service.prepare_turn(fixture.user, handle, 'Legacy default')
    assert [event async for event in turn][-1] == {'type': 'turn_complete'}
    await service.close(fixture.user, handle)
    service.store.rows[cid].pop('reasoning_effort')
    row = (await service.get_conversation(fixture.user, cid))['conversation']
    assert row['reasoning_effort'] is None
    await service.resume(fixture.user, cid, row['revision'])
    assert not hasattr(fixture.layer.started[-1][1], 'reasoning_effort')


def usage_report():
    return dict(type='usage', event_id=str(uuid.uuid4()), reported_model='reported-model',
                input_tokens=100, output_tokens=5, cache_read_tokens=0, cache_write_tokens=None,
                reasoning_tokens=2, reported_nano_aiu=None)


@pytest.mark.asyncio
async def test_usage_is_durable_before_stream_delivery_and_completion(fixture, monkeypatch):
    service = fixture.service()
    sid = await create(fixture, service)
    entry = service._entries[sid]
    report = usage_report()
    began, release = threading.Event(), threading.Event()
    original = service.store.append_usage

    def held(*args):
        began.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(service.store, 'append_usage', held)

    async def program(sid):
        fixture.layer.usage_observers[sid](report)
        yield CommonEvent('text', {'content': 'work'})
        yield CommonEvent('done')

    fixture.layer.program = program
    turn = await service.prepare_turn(fixture.user, sid, 'work')

    async def collect():
        frames = []
        async for frame in turn:
            if frame['type'] == 'usage':
                assert any(event.get('event_id') == report['event_id']
                           for event in service.store.events(entry.cid, fixture.user.sub))
            frames.append(frame)
        return frames

    reading = asyncio.create_task(collect())
    try:
        async with asyncio.timeout(1):
            while not began.is_set():
                await asyncio.sleep(0.001)
        assert not reading.done()
        assert all(frame['type'] != 'usage' for frame in service.store.events(entry.cid, fixture.user.sub))
    finally:
        release.set()
    frames = await reading
    assert [frame for frame in frames if frame['type'] == 'usage'] == [report]
    assert frames[-1]['type'] == 'turn_complete'


@pytest.mark.asyncio
async def test_late_idle_and_shutdown_usage_are_saved_without_another_turn(fixture):
    service = fixture.service()
    sid = await create(fixture, service)
    entry = service._entries[sid]
    frames = [frame async for frame in await service.prepare_turn(fixture.user, sid, 'work')]
    first, last = usage_report(), usage_report()
    observer = fixture.layer.usage_observers[sid]
    observer(first)
    observer(first.copy())
    await service._flush_usage(entry)

    async def close_hook(closing_sid):
        assert closing_sid == sid
        observer(last)

    fixture.layer.close_hook = close_hook
    await service.close(fixture.user, sid)
    saved = await service.get_conversation(fixture.user, entry.cid)
    reports = [event for event in saved['events'] if event['type'] == 'usage']
    assert [{key: value for key, value in report.items() if key != 'seq'} for report in reports] == [first, last]
    assert saved['conversation']['can_resume']
    assert frames[-1]['type'] == 'turn_complete'
    assert len(fixture.layer.messages) == 1
    assert entry.usage_task.done() and entry.usage_sealed


@pytest.mark.asyncio
async def test_usage_survives_cold_resume_without_duplicate_or_stale_writer(fixture):
    store = MemoryConversations()
    first = fixture.service(store=store)
    sid = await create(fixture, first)
    original = first._entries[sid]
    old_observer = fixture.layer.usage_observers[sid]
    _ = [event async for event in await first.prepare_turn(fixture.user, sid, 'work')]
    report = usage_report()
    old_observer(report)
    await first.close(fixture.user, sid)
    saved = await first.get_conversation(fixture.user, original.cid)
    second = fixture.service(store=store)
    handle = await second.resume(fixture.user, original.cid, saved['conversation']['revision'])
    resumed = second._entry(fixture.user, handle)
    with pytest.raises(ValueError):
        old_observer(usage_report())
    fixture.layer.usage_observers[sid](report)
    fresh = usage_report()
    fixture.layer.usage_observers[sid](fresh)
    await second._flush_usage(resumed)
    rows = await second.get_conversation(fixture.user, original.cid)
    assert [event['event_id'] for event in rows['events'] if event['type'] == 'usage'] == [report['event_id'], fresh['event_id']]
    assert resumed.closing is None and resumed.user.sub == original.user.sub
    assert resumed.account_id == original.account_id and resumed.model == original.model
    with pytest.raises(CopilotChatError) as denied:
        await second.get_conversation(fixture.other, original.cid)
    assert denied.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['database', 'conflicting_id', 'malformed', 'overflow'])
async def test_failed_usage_closes_and_quarantines_owner(fixture, monkeypatch, failure):
    service = fixture.service()
    sid = await create(fixture, service)
    entry = service._entries[sid]
    _ = [event async for event in await service.prepare_turn(fixture.user, sid, 'work')]
    observer = fixture.layer.usage_observers[sid]
    report = usage_report()
    if failure == 'database':
        def broken(*args):
            raise RuntimeError('private provider material')
        monkeypatch.setattr(service.store, 'append_usage', broken)
        observer(report)
    elif failure == 'conflicting_id':
        observer(report)
        await service._flush_usage(entry)
        observer({**report, 'input_tokens': 101})
    elif failure == 'malformed':
        with pytest.raises(ValueError, match='observation is unavailable'):
            observer({**report, 'input_tokens': True})
    else:
        with pytest.raises(ValueError, match='observation is unavailable'):
            for _ in range(129):
                observer(usage_report())
    await gone(fixture, service, sid)
    saved = await service.get_conversation(fixture.user, entry.cid)
    assert saved['conversation']['state'] == 'incomplete'
    assert not saved['conversation']['can_resume']
    assert entry.usage_task.done() and entry.usage_failed


@pytest.mark.asyncio
async def test_failed_layer_cleanup_drains_confirmed_closed_usage_source_but_retains_claim(fixture, monkeypatch):
    service = fixture.service()
    sid = await create(fixture, service)
    entry = service._entries[sid]
    report = usage_report()

    async def failed_close(closing_sid):
        fixture.layer.usage_observers[closing_sid](report)
        fixture.layer.sessions.pop(closing_sid)
        raise RuntimeError('An ownership tombstone remains after native shutdown')

    async def claimed(_sid):
        return False

    fixture.layer.close_hook = failed_close
    monkeypatch.setattr(fixture.layer, 'is_session_process_dead', claimed)
    with pytest.raises(CopilotChatError):
        await service.close(fixture.user, sid)
    assert service._entries[sid] is entry and sid in fixture.slots
    assert entry.usage_sealed and entry.usage_task.done()
    assert service.store.events(entry.cid, fixture.user.sub) == [{**report, 'seq': 1}]


@pytest.mark.asyncio
async def test_cancelled_close_waits_for_accepted_usage_write(fixture, monkeypatch):
    service = fixture.service()
    sid = await create(fixture, service)
    entry = service._entries[sid]
    _ = [event async for event in await service.prepare_turn(fixture.user, sid, 'complete')]
    began, release = threading.Event(), threading.Event()
    original = service.store.append_usage

    def held(*args):
        began.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(service.store, 'append_usage', held)
    report = usage_report()
    fixture.layer.usage_observers[sid](report)
    closing = None
    try:
        async with asyncio.timeout(1):
            while not began.is_set():
                await asyncio.sleep(0.001)
        closing = asyncio.create_task(service.close(fixture.user, sid))
        async with asyncio.timeout(1):
            while not entry.usage_sealed:
                await asyncio.sleep(0.001)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done() and not entry.usage_task.done()
        assert service._entries[sid] is entry
    finally:
        release.set()
        if closing is not None:
            result = await asyncio.gather(closing, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert sid not in service._entries and entry.usage_task.done()
    saved = await service.get_conversation(fixture.user, entry.cid)
    assert [event['event_id'] for event in saved['events'] if event['type'] == 'usage'] == [report['event_id']]
    assert saved['conversation']['can_resume']


@pytest_asyncio.fixture
async def delegation(fixture, monkeypatch):
    workers = []
    control = SimpleNamespace(hold=None, close_hold=None, close_error=False)

    class Worker:
        def __init__(self, **options):
            self.options = options
            self.closed = False
            self.identity = dict(task_id="task-1", run_id="run-1", chat_id="chat-1",
                                 agent=options["target_agent"])
            workers.append(self)

        async def run(self):
            await self.options["authorize_parent"]()
            await self.options["publish"]({**self.identity, "type": "delegate_spawn",
                                           "name": self.options["name"], "tool_id": self.options["tool_call_id"]})
            if control.hold:
                await control.hold.wait()
            return {**self.identity, "status": "completed", "output": "Reviewed repository"}

        async def close(self):
            if control.close_hold:
                await control.close_hold.wait()
            if control.close_error:
                raise RuntimeError("private child cleanup detail")
            self.closed = True
            if control.hold:
                control.hold.set()

    monkeypatch.setitem(sys.modules, "services.delegation.copilot_worker", SimpleNamespace(OwnedCopilotWorker=Worker))
    service = fixture.service()
    sid = await service.create(fixture.user, "agent", "account", "model", delegation_enabled=True)
    return SimpleNamespace(service=service, sid=sid, workers=workers, control=control,
                           args={"agent": "repo", "name": "Review repo", "prompt": "Review the repository."})


@pytest.mark.asyncio
async def test_owned_delegation_persists_before_stream_and_resume_never_replays(fixture, delegation):
    d = delegation
    async def program(sid):
        result = await fixture.layer.delegate_handlers[sid]("native-call", d.args)
        assert "Reviewed repository" in result
        assert d.workers[0].closed
        yield CommonEvent("text", {"content": "Review complete"})
        yield CommonEvent("done", {})
    fixture.layer.program = program
    turn = await d.service.prepare_turn(fixture.user, d.sid, "Delegate review")
    frames = []
    async for frame in turn:
        if frame["type"].startswith("delegate_"):
            saved = d.service.store.events(d.service.conversation_id(fixture.user, d.sid), fixture.user.sub)
            assert any({k: v for k, v in row.items() if k != "seq"} == frame for row in saved)
        frames.append(frame)
    await turn.aclose()
    assert [frame["type"] for frame in frames] == ["delegate_spawn", "delegate_result", "text", "turn_complete"]
    cid = d.service.conversation_id(fixture.user, d.sid)
    assert not d.service._entries[d.sid].workers
    await d.service.close(fixture.user, d.sid)
    row = d.service.store.get(cid, fixture.user.sub)
    resumed = await d.service.resume(fixture.user, cid, row["revision"])
    assert d.service._entry(fixture.user, resumed).delegation_enabled is True
    async def replay(sid):
        with pytest.raises(CopilotChatError, match="already requested"):
            await fixture.layer.delegate_handlers[sid]("native-call", d.args)
        yield CommonEvent("done", {})
    fixture.layer.program = replay
    assert [frame async for frame in await d.service.prepare_turn(fixture.user, resumed, "Continue")] == [{"type": "turn_complete"}]
    assert len(d.workers) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["roster", "reservation", "revoked"])
async def test_delegation_denial_precedes_worker_construction(fixture, delegation, monkeypatch, failure):
    d = delegation
    if failure == "roster":
        d.args["agent"] = "unauthorized"
    if failure == "reservation":
        monkeypatch.setattr(d.service.store, "reserve_delegation", lambda *args: False)
    async def program(sid):
        if failure == "revoked":
            fixture.current["revision"] += 1
        with pytest.raises(CopilotChatError):
            await fixture.layer.delegate_handlers[sid]("native-call", d.args)
        yield CommonEvent("done", {})
    fixture.layer.program = program
    turn = await d.service.prepare_turn(fixture.user, d.sid, "Delegate")
    _ = [frame async for frame in turn]
    await turn.aclose()
    assert d.workers == []


@pytest.mark.asyncio
async def test_parent_close_waits_for_owned_worker_cleanup(fixture, delegation):
    d = delegation
    d.control.hold, d.control.close_hold = asyncio.Event(), asyncio.Event()
    async def program(sid):
        await fixture.layer.delegate_handlers[sid]("native-call", d.args)
        yield CommonEvent("done", {})
    fixture.layer.program = program
    turn = await d.service.prepare_turn(fixture.user, d.sid, "Delegate")
    assert (await anext(turn))["type"] == "delegate_spawn"
    closing = asyncio.create_task(d.service.close(fixture.user, d.sid))
    await asyncio.sleep(0.01)
    assert not closing.done() and d.sid in d.service._entries and d.sid in fixture.slots
    d.control.close_hold.set()
    await asyncio.wait_for(closing, 1)
    assert d.workers[0].closed and d.sid not in d.service._entries


@pytest.mark.asyncio
async def test_parent_delegation_cap_is_reserved_before_async_storage(fixture, delegation, monkeypatch):
    d = delegation
    d.control.hold = asyncio.Event()
    entered, release = threading.Event(), threading.Event()
    reserve = d.service.store.reserve_delegation
    def held(*args):
        entered.set()
        assert release.wait(2)
        return reserve(*args)
    monkeypatch.setattr(d.service.store, "reserve_delegation", held)
    async def program(sid):
        calls = [asyncio.create_task(fixture.layer.delegate_handlers[sid](f"call-{i}", d.args)) for i in range(4)]
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            with pytest.raises(CopilotChatError, match="unavailable"):
                await fixture.layer.delegate_handlers[sid]("overflow", d.args)
            assert not d.workers
        finally:
            release.set()
            d.control.hold.set()
        await asyncio.gather(*calls)
        yield CommonEvent("done", {})
    fixture.layer.program = program
    frames = [frame async for frame in await d.service.prepare_turn(fixture.user, d.sid, "Four tasks")]
    assert sum(frame["type"] == "delegate_result" for frame in frames) == 4
    assert len(d.workers) == 4 and all(worker.closed for worker in d.workers)


@pytest.mark.asyncio
async def test_result_storage_failure_joins_worker_without_publishing_result(fixture, delegation, monkeypatch):
    d = delegation
    append = d.service.store.append_event
    def fail_result(cid, owner, generation, frame):
        if frame["type"] == "delegate_result":
            raise RuntimeError("private database detail")
        return append(cid, owner, generation, frame)
    monkeypatch.setattr(d.service.store, "append_event", fail_result)
    async def program(sid):
        await fixture.layer.delegate_handlers[sid]("call", d.args)
        yield CommonEvent("done", {})
    fixture.layer.program = program
    cid = d.service.conversation_id(fixture.user, d.sid)
    frames = [frame async for frame in await d.service.prepare_turn(fixture.user, d.sid, "Review")]
    assert not any(frame["type"] in {"delegate_result", "turn_complete"} for frame in frames)
    assert d.workers[0].closed
    assert not any(frame["type"] == "delegate_result" for frame in d.service.store.events(cid, fixture.user.sub))


@pytest.mark.asyncio
async def test_unproven_worker_cleanup_retains_parent_and_quarantines_completion(fixture, delegation):
    d = delegation
    d.control.close_error = True
    async def program(sid):
        await fixture.layer.delegate_handlers[sid]("call", d.args)
        yield CommonEvent("done", {})
    fixture.layer.program = program
    turn = await d.service.prepare_turn(fixture.user, d.sid, "Review")
    frames = [frame async for frame in turn]
    assert not any(frame["type"] in {"delegate_result", "turn_complete"} for frame in frames)
    entry = d.service._entries[d.sid]
    assert entry.workers["call"] is d.workers[0]
    assert d.sid in fixture.slots
    with pytest.raises(CopilotChatError, match="cleanup is incomplete"):
        await d.service.close(fixture.user, d.sid)
    assert "private" not in str(frames)
    d.control.close_error = False



@pytest.mark.asyncio
async def test_parent_close_unblocks_worker_publication_when_stream_queue_is_full(fixture, delegation, monkeypatch):
    d = delegation
    module = sys.modules["services.delegation.copilot_worker"]
    class StartupOwner(module.OwnedCopilotWorker):
        async def run(self):
            self.startup = asyncio.create_task(super().run())
            return await asyncio.shield(self.startup)
        async def close(self):
            # Like the real scheduler owner, join startup instead of cancelling
            # a coroutine that may own an uncancellable database mutation.
            await asyncio.gather(self.startup, return_exceptions=True)
            self.closed = True
    monkeypatch.setattr(module, "OwnedCopilotWorker", StartupOwner)
    ready = asyncio.Event()
    async def program(sid):
        entry = d.service._entries[sid]
        # A slow/lost reader has filled the bounded stream before the child
        # startup publisher can return. The producer waits for that child.
        for _ in range(128):
            entry.turn._queue.put_nowait({"type": "text", "content": "buffered"})
        ready.set()
        await fixture.layer.delegate_handlers[sid]("call", d.args)
        yield CommonEvent("done", {})
    fixture.layer.program = program
    turn = await d.service.prepare_turn(fixture.user, d.sid, "Review")
    await ready.wait()
    async with asyncio.timeout(1):
        while not d.workers:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(d.service.close(fixture.user, d.sid), 2)
    assert d.workers[0].closed and d.sid not in d.service._entries
    assert turn._producer.done()

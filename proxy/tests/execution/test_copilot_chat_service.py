"""Personal preview admission, real human queues, and owned producer teardown."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
import uuid

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

    async def start_session(self, sid, config):
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
        return SimpleNamespace(revision=current["revision"], **values)

    async def acquire(sid, **values):
        assert values["target"] == "local" and values["execution_path"] == "copilot-cli"
        if current["admission"]:
            slots.add(sid)
        return current["admission"]

    monkeypatch.setattr(module, "build_copilot_agent_config", build)
    monkeypatch.setattr(module, "acquire_chat_slot", acquire)
    monkeypatch.setattr(module, "release_chat_slot", slots.discard)

    def service(**options):
        options.setdefault("watch_interval", 0.01)
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

    async def refused(sid, _config):
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

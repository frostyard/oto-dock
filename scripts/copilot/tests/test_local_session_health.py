"""Factory health failures cannot be hidden by a previous CommonEvent DONE."""

import asyncio

import pytest

from test_local_session_factory import clean, documents, harness as harness, open_session


@pytest.mark.asyncio
async def test_post_done_supervisor_failure_never_commits_ready_history(harness):
    owner = await open_session(harness)
    try:
        events = [event async for event in owner.stream("completed turn")]
        assert events[-1].type == "done"
        sdk = harness.runtimes[0].client.opened[0]
        sdk.emit("tool.execution_start", {"toolCallId": "malformed-missing-name"})
        assert owner._supervisor.failure_detected is True
        await owner.close()
        assert documents(harness)[0]["status"] == "active"
        assert not harness.guards[0].valid
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_session_error_followed_by_idle_and_done_never_commits_ready_history(harness):
    owner = await open_session(harness)
    sdk = harness.runtimes[0].client.opened[0]
    original = sdk.send

    async def failed(prompt, *, mode):
        result = await original(prompt, mode=mode)
        sdk.emit("session.error", {"message": "model turn failed"})
        sdk.emit("session.idle")
        return result

    sdk.send = failed
    try:
        events = [event async for event in owner.stream("failed turn")]
        assert any(event.type == "error" for event in events)
        assert "model turn failed" not in repr(events)
        assert "Copilot provider request failed" in repr(events)
        assert any(event.type == "done" for event in events)
        await owner.close()
        assert documents(harness)[0]["status"] == "active"
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_session_error_after_done_is_sticky_without_another_consumer(harness):
    owner = await open_session(harness)
    try:
        events = [event async for event in owner.stream("completed turn")]
        assert events[-1].type == "done"
        sdk = harness.runtimes[0].client.opened[0]
        sdk.emit("session.error", {"message": "error after consumer stopped"})
        await owner.close()
        assert documents(harness)[0]["status"] == "active"
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [False, True])
async def test_runtime_death_closes_lease_without_waiting_for_idle_or_paused_consumer(harness, paused):
    owner = await open_session(harness)
    stream = owner.stream("fixture turn")
    try:
        if paused:
            first = await anext(stream)
            assert first.type != "done"
        else:
            events = [event async for event in stream]
            assert events[-1].type == "done"
        runtime = harness.runtimes[0]
        runtime.alive = False  # Exact runtime owner has observed transport/process loss.
        await asyncio.wait_for(runtime.closed_event.wait(), 2)
        await asyncio.wait_for(owner.close(), 2)
        assert not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
        if paused:
            with pytest.raises(harness.module.CopilotLocalSessionError) as error:
                await anext(stream)
            assert error.value.__context__ is None
    finally:
        await stream.aclose()
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_startup_session_error_cannot_publish_open_owner(harness):
    async def emit_error():
        harness.runtimes[0].client.opened[-1].emit("session.error", {"message": "startup error"})

    harness.stages["sdk.create"] = emit_error
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError) as error:
            await open_session(harness)
        assert error.value.__context__ is None
        assert harness.runtimes[0].closed and not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)


@pytest.mark.asyncio
async def test_supervisor_failure_health_flag_is_readonly_and_survives_cleanup(harness):
    owner = await open_session(harness)
    try:
        assert owner._supervisor.failure_detected is False
        with pytest.raises(AttributeError):
            owner._supervisor.failure_detected = False
        sdk = harness.runtimes[0].client.opened[0]
        sdk.emit("tool.execution_start", {"toolCallId": "malformed"})
        assert owner._supervisor.failure_detected is True
        await owner.close()
        assert owner._supervisor.failure_detected is True
        assert documents(harness)[0]["status"] == "active"
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_lifecycle_properties_are_readonly_and_safe_without_caller_task(harness):
    owner = await open_session(harness)
    try:
        assert owner.alive is True and owner.closed is False
        with pytest.raises(AttributeError):
            owner.alive = False
        with pytest.raises(AttributeError):
            owner.closed = True
        # A worker thread has no running event loop/current asyncio task.
        assert await asyncio.to_thread(lambda: (owner.alive, owner.closed)) == (True, False)
        await owner.close()
        assert owner.alive is False and owner.closed is True
        assert await asyncio.to_thread(lambda: (owner.alive, owner.closed)) == (False, True)
        await owner.wait_closed()  # Late subscription returns immediately.
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_wait_closed_observes_without_initiating_shutdown(harness):
    owner = await open_session(harness)
    started = asyncio.Event()

    async def observe():
        started.set()
        await owner.wait_closed()

    waiter = asyncio.create_task(observe())
    try:
        await started.wait()
        assert not waiter.done()
        assert owner.alive is True and owner.closed is False
        assert harness.runtimes[0].closed is False
        await owner.close()
        await asyncio.wait_for(waiter, 1)
        assert owner.closed is True
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_cancel_waiter_before_closing_does_not_close_owner_or_other_waiters(harness):
    owner = await open_session(harness)
    started = asyncio.Event()

    async def observe():
        started.set()
        await owner.wait_closed()

    first = asyncio.create_task(observe())
    second = asyncio.create_task(owner.wait_closed())
    try:
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert owner.alive is True and owner.closed is False
        assert not second.done()
        await owner.close()
        await asyncio.wait_for(second, 1)
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_cancel_waiter_during_closing_does_not_cancel_owned_cleanup(harness):
    owner = await open_session(harness)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_close():
        entered.set()
        await release.wait()

    harness.stages["runtime.close"] = held_close
    closing = asyncio.create_task(owner.close())
    first = asyncio.create_task(owner.wait_closed())
    second = asyncio.create_task(owner.wait_closed())
    try:
        await entered.wait()
        assert owner.alive is False and owner.closed is False
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not closing.done() and not second.done()
        assert harness.runtimes[0].closed is False
        release.set()
        await asyncio.wait_for(asyncio.gather(closing, second), 1)
        assert owner.closed is True
        await owner.wait_closed()
    finally:
        release.set()
        await asyncio.gather(closing, first, second, return_exceptions=True)
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_failed_cleanup_is_closed_but_waiters_receive_sanitized_failure(harness):
    owner = await open_session(harness)

    async def failed_close():
        raise RuntimeError("private runtime cleanup payload")

    harness.stages["runtime.close"] = failed_close
    waiting = asyncio.create_task(owner.wait_closed())
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await owner.close()
        assert owner.alive is False and owner.closed is True
        for waiter in (waiting, owner.wait_closed()):
            with pytest.raises(harness.module.CopilotLocalSessionError) as error:
                await waiter
            assert "private" not in str(error.value)
            assert error.value.__context__ is None and error.value.__cause__ is None
        assert documents(harness)[0]["status"] == "active"
    finally:
        await asyncio.gather(waiting, return_exceptions=True)
        await clean(harness)


@pytest.mark.asyncio
async def test_independent_owner_death_wakes_wait_closed_and_cleans_record(harness):
    owner = await open_session(harness)
    waiting = asyncio.create_task(owner.wait_closed())
    try:
        harness.runtimes[0].alive = False
        assert owner.alive is False and owner.closed is False
        await asyncio.wait_for(waiting, 2)
        assert owner.closed is True and not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_alive_rejects_context_failure_without_raising_or_starting_cleanup(harness, monkeypatch):
    owner = await open_session(harness)

    def unavailable():
        raise RuntimeError("private context lookup")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, "_context_valid", unavailable)
            assert owner.alive is False
            assert owner.closed is False
            assert harness.runtimes[0].closed is False
        assert owner.alive is True
    finally:
        await owner.close()
        await clean(harness)


@pytest.mark.asyncio
async def test_lifecycle_indicators_cover_partial_startup_and_its_joined_cleanup(harness, monkeypatch):
    seen = []
    original = harness.module.CopilotLocalSession._open

    async def capture(instance, *args, **kwargs):
        seen.append(instance)
        assert instance.alive is False and instance.closed is False
        return await original(instance, *args, **kwargs)

    async def fail_start():
        assert seen[0].alive is False and seen[0].closed is False
        raise RuntimeError("private partial startup failure")

    monkeypatch.setattr(harness.module.CopilotLocalSession, "_open", capture)
    harness.stages["runtime.start"] = fail_start
    try:
        with pytest.raises(harness.module.CopilotLocalSessionError):
            await open_session(harness)
        assert len(seen) == 1
        assert seen[0].alive is False and seen[0].closed is True
        await seen[0].wait_closed()
        assert harness.runtimes[0].closed and not harness.guards[0].valid
        assert documents(harness)[0]["status"] == "active"
    finally:
        await clean(harness)

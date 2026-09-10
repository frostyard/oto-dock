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

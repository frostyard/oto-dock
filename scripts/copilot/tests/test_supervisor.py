"""Session orchestration races using an adversarial SDK-independent backend."""

import asyncio
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.supervisor import (  # noqa: E402
    CopilotSessionSupervisor, RuntimeSnapshot, SessionSupervisorError,
)
from core.layers.copilot.coordinator import TaskObservation, TaskState  # noqa: E402

EMPTY = RuntimeSnapshot(False, (), frozenset(), frozenset())


class Backend:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.serial = 0
        self.messages = 0
        self.state = EMPTY
        self.disconnected = False
        self.snapshot_calls = 0
        self.barrier_calls = 0
        self.send_hook = None
        self.snapshot_hook = None
        self.interrupt_accepted = True
        self.sent = asyncio.Event()

    def emit(self, kind, data=None):
        self.serial += 1
        self.supervisor.receive_event({"id": f"event-{self.serial}", "type": kind, "data": data or {}})

    async def send(self, prompt, *, immediate=False):
        self.messages += 1
        message_id = f"message-{self.messages}"
        self.emit("user.message", {"messageId": message_id, "content": prompt})
        self.emit("assistant.turn_start", {"turnId": f"turn-{self.messages}"})
        self.sent.set()
        if self.send_hook:
            await self.send_hook(message_id)
        else:
            self.emit("assistant.message", {"messageId": f"reply-{self.messages}", "content": "reply"})
            self.emit("session.idle")
        return message_id

    async def abort(self):
        self.state = EMPTY
        self.emit("session.idle", {"aborted": True})

    async def interrupt(self):
        if self.interrupt_accepted:
            self.state = replace(self.state, processing=False)
            self.emit("assistant.idle")
        return self.interrupt_accepted

    async def snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_hook:
            await self.snapshot_hook()
        return self.state

    async def is_processing(self):
        self.barrier_calls += 1
        return self.state.processing

    async def disconnect(self):
        self.disconnected = True


def make(**options):
    closed = []

    async def close_runtime():
        closed.append(True)

    supervisor = CopilotSessionSupervisor(
        pending_requests=options.pop("pending_requests", frozenset),
        close_runtime=close_runtime, rpc_timeout=options.pop("rpc_timeout", 0.2),
        turn_timeout=options.pop("turn_timeout", 2), **options,
    )
    backend = Backend(supervisor)
    supervisor.bind(backend)
    return supervisor, backend, closed


async def collect(supervisor, prompt="hello"):
    return [event async for event in supervisor.stream(prompt)]


async def hold_backend(supervisor, backend):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    owned = []

    async def callback():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def send_hook(_message_id):
        backend.state = replace(EMPTY, processing=True)
        backend.emit("tool.execution_start", {"toolCallId": "held", "toolName": "fixture", "arguments": {}})
        task = asyncio.create_task(supervisor.callbacks.run("held", callback))
        # Await cancellation to consume its exception in fixture teardown.
        owned.append(task)

    backend.send_hook = send_hook
    producer = asyncio.create_task(collect(supervisor))
    await asyncio.wait_for(started.wait(), 1)
    return producer, cancelled, owned


@pytest.mark.asyncio
async def test_two_turns_reconcile_delivery_before_ack_without_replaying_completion():
    supervisor, backend, closed = make()
    try:
        first = await collect(supervisor)
        second = await collect(supervisor)
        assert [e.type for e in first].count("done") == 1
        assert [e.type for e in second].count("done") == 1
        assert backend.messages == 2 and backend.snapshot_calls == 2
        assert not closed
    finally:
        await supervisor.close()
    assert closed == [True] and backend.disconnected


@pytest.mark.asyncio
async def test_abort_joins_host_callback_before_error_tool_result_and_done():
    supervisor, backend, closed = make()
    producer, cancelled, owned = await hold_backend(supervisor, backend)
    try:
        acknowledgement = await supervisor.abort()
        result = await asyncio.wait_for(producer, 1)
        assert acknowledgement.accepted and acknowledgement.callbacks_stopped
        assert cancelled.is_set() and not supervisor.callbacks.pending_ids
        tool = next(event for event in result if event.type == "tool_result")
        assert tool.data["is_error"] is True
        assert result[-1].type == "done"
    finally:
        await supervisor.close()
        await asyncio.gather(*owned, return_exceptions=True)
    assert closed == [True]


@pytest.mark.asyncio
async def test_interrupt_finishes_without_native_idle_only_after_background_retirement():
    supervisor, backend, _ = make()
    producer, cancelled, owned = await hold_backend(supervisor, backend)
    backend.state = replace(backend.state, tasks=(TaskObservation("bg", TaskState.RUNNING),))
    try:
        acknowledgement = await supervisor.interrupt()
        assert acknowledgement.accepted and cancelled.is_set()
        await asyncio.sleep(0.15)
        assert not producer.done()
        backend.state = EMPTY  # No event: exercise bounded snapshot polling.
        result = await asyncio.wait_for(producer, 1)
        assert result[-1].type == "done" and backend.barrier_calls >= 1
        assert not any(e.type == "system" and e.data.get("subtype") == "copilot_idle" for e in result)
    finally:
        await supervisor.close()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_host_permission_state_never_finishes_from_native_idle():
    supervisor, backend, _ = make(pending_requests=lambda: None)
    producer = asyncio.create_task(collect(supervisor))
    await backend.sent.wait()
    await asyncio.sleep(0.12)
    assert not producer.done()
    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer
    assert backend.disconnected


@pytest.mark.asyncio
async def test_new_event_during_snapshot_invalidates_idle_without_poll_spin():
    supervisor, backend, _ = make()

    async def invalidate():
        backend.emit("session.background_tasks_changed")

    backend.snapshot_hook = invalidate
    producer = asyncio.create_task(collect(supervisor))
    await backend.sent.wait()
    await asyncio.sleep(0.2)
    assert backend.snapshot_calls == 1 and not producer.done()
    backend.snapshot_hook = None
    backend.emit("session.idle")
    assert (await asyncio.wait_for(producer, 1))[-1].type == "done"
    await supervisor.close()


@pytest.mark.asyncio
async def test_stream_cancellation_closes_runtime_and_owned_callback():
    supervisor, backend, closed = make()
    producer, cancelled, owned = await hold_backend(supervisor, backend)
    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer
    await asyncio.gather(*owned, return_exceptions=True)
    assert cancelled.is_set() and backend.disconnected and closed == [True]
    await supervisor.close()
    assert closed == [True]


@pytest.mark.asyncio
async def test_send_ack_failure_is_uncertain_sanitized_and_never_retried():
    supervisor, backend, closed = make()

    async def fail(_message_id):
        raise RuntimeError("token-must-not-appear")

    backend.send_hook = fail
    with pytest.raises(SessionSupervisorError, match="acceptance is uncertain") as exc:
        await collect(supervisor)
    assert "token-must-not-appear" not in str(exc.value)
    assert backend.messages == 1 and closed == [True]


@pytest.mark.asyncio
async def test_overflow_fails_closed_and_stops_runtime():
    supervisor, backend, closed = make(queue_capacity=1)
    with pytest.raises(SessionSupervisorError, match="bounded buffer"):
        await collect(supervisor)
    assert closed == [True] and backend.disconnected


@pytest.mark.asyncio
async def test_second_consumer_and_steering_after_done_are_rejected():
    supervisor, backend, _ = make()
    stream = supervisor.stream("first")
    first = await anext(stream)
    assert first.type != "done"
    with pytest.raises(SessionSupervisorError, match="already owns"):
        await collect(supervisor)
    async for event in stream:
        if event.type == "done":
            with pytest.raises(SessionSupervisorError, match="No active"):
                await supervisor.steer("too late")
    assert backend.messages == 1
    await supervisor.close()


@pytest.mark.asyncio
async def test_close_stops_runtime_even_if_disconnect_raises_private_error():
    supervisor, backend, closed = make()

    async def fail_disconnect():
        raise RuntimeError("private-token")

    backend.disconnect = fail_disconnect
    with pytest.raises(SessionSupervisorError, match="cleanup is incomplete"):
        await supervisor.close()
    assert closed == [True]


@pytest.mark.asyncio
async def test_steering_waiting_for_writer_is_rejected_after_stream_finishes():
    supervisor, backend, _ = make()
    stream = supervisor.stream("first")
    await anext(stream)
    try:
        async with supervisor.coordinator.writer():
            steering = asyncio.create_task(supervisor.steer("too late"))
            await asyncio.sleep(0)
            assert [event async for event in stream][-1].type == "done"
        with pytest.raises(SessionSupervisorError, match="No active"):
            await steering
        assert backend.messages == 1
        assert (await collect(supervisor))[-1].type == "done"
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_steering_for_previous_stream_cannot_target_new_consumer():
    supervisor, backend, _ = make()
    stream = supervisor.stream("first")
    await anext(stream)
    try:
        async with supervisor.coordinator.writer():
            steering = asyncio.create_task(supervisor.steer("previous stream"))
            await asyncio.sleep(0)
            assert [event async for event in stream][-1].type == "done"
            next_stream = asyncio.create_task(collect(supervisor, "new stream"))
            await asyncio.sleep(0)
        with pytest.raises(SessionSupervisorError, match="No active"):
            await steering
        assert (await next_stream)[-1].type == "done"
        assert backend.messages == 2
    finally:
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["abort", "interrupt"])
async def test_control_waiting_for_writer_cannot_poison_completed_session(control):
    supervisor, backend, _ = make()
    stream = supervisor.stream("first")
    await anext(stream)
    try:
        async with supervisor.coordinator.writer():
            pending = asyncio.create_task(getattr(supervisor, control)())
            await asyncio.sleep(0)
            assert [event async for event in stream][-1].type == "done"
        with pytest.raises(SessionSupervisorError, match="No active"):
            await pending
        # Rejecting a stale control request must not invalidate the session.
        assert (await collect(supervisor))[-1].type == "done"
        assert backend.messages == 2
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_overflow_stops_runtime_without_resuming_paused_consumer():
    closed = asyncio.Event()

    async def close_runtime():
        closed.set()

    supervisor = CopilotSessionSupervisor(
        pending_requests=frozenset, close_runtime=close_runtime, queue_capacity=8,
    )
    backend = Backend(supervisor)
    supervisor.bind(backend)
    stream = supervisor.stream("first")
    await anext(stream)
    try:
        for _ in range(20):
            backend.emit("assistant.message_delta", {"messageId": "next", "deltaContent": "x"})
        await asyncio.wait_for(closed.wait(), 1)
        assert backend.disconnected
        with pytest.raises(SessionSupervisorError, match="bounded buffer"):
            await anext(stream)
    finally:
        await stream.aclose()
        await supervisor.close()


@pytest.mark.asyncio
async def test_repeated_close_cancellation_cannot_interrupt_owned_cleanup():
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def close_runtime():
        entered.set()
        await release.wait()
        stopped.set()

    supervisor = CopilotSessionSupervisor(
        pending_requests=frozenset, close_runtime=close_runtime,
    )
    closer = asyncio.create_task(supervisor.close())
    await entered.wait()
    closer.cancel()
    await asyncio.sleep(0)
    closer.cancel()
    await asyncio.sleep(0)
    assert not stopped.is_set() and not closer.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closer
    assert stopped.is_set()
    await supervisor.close()


@pytest.mark.asyncio
async def test_cleanup_error_does_not_replace_original_stream_failure():
    supervisor, backend, closed = make(queue_capacity=1)

    async def fail_disconnect():
        raise RuntimeError("private-disconnect-detail")

    backend.disconnect = fail_disconnect
    with pytest.raises(SessionSupervisorError, match="bounded buffer"):
        await collect(supervisor)
    assert closed == [True]
    with pytest.raises(SessionSupervisorError, match="cleanup is incomplete"):
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    replace(EMPTY, processing=0), replace(EMPTY, processing="false"),
    replace(EMPTY, pending_permissions=set()), replace(EMPTY, pending_messages=set()),
])
async def test_malformed_snapshot_is_not_coerced_into_settlement(state):
    supervisor, backend, closed = make()
    backend.state = state
    with pytest.raises(SessionSupervisorError, match="settlement could not be established"):
        await collect(supervisor)
    assert closed == [True]


@pytest.mark.asyncio
async def test_queued_completion_rejects_steering_before_done_is_consumed():
    supervisor, backend, _ = make()
    started = asyncio.Event()
    owned = []

    async def callback():
        started.set()
        await asyncio.Event().wait()

    async def send_hook(_message_id):
        backend.state = replace(EMPTY, processing=True)
        backend.emit("tool.execution_start", {"toolCallId": "held", "toolName": "fixture", "arguments": {}})
        owned.append(asyncio.create_task(supervisor.callbacks.run("held", callback)))

    backend.send_hook = send_hook
    stream = supervisor.stream("first")
    await anext(stream)
    await started.wait()
    try:
        await supervisor.abort()
        saw_tool_result = False
        async for event in stream:
            if event.type == "tool_result":
                saw_tool_result = True
                with pytest.raises(SessionSupervisorError, match="No active"):
                    await supervisor.steer("after reconciliation")
        assert saw_tool_result and backend.messages == 1
    finally:
        await supervisor.close()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [1, "true", None])
async def test_nonboolean_interrupt_acknowledgement_is_not_accepted(accepted):
    supervisor, backend, closed = make()
    producer, _, owned = await hold_backend(supervisor, backend)
    backend.interrupt_accepted = accepted
    with pytest.raises(SessionSupervisorError, match="control outcome is uncertain"):
        await supervisor.interrupt()
    with pytest.raises(SessionSupervisorError, match="control outcome is uncertain"):
        await producer
    await asyncio.gather(*owned, return_exceptions=True)
    assert closed == [True]


@pytest.mark.asyncio
async def test_start_is_rejected_as_soon_as_close_has_been_requested():
    supervisor, backend, closed = make()
    closing = asyncio.create_task(supervisor.close())
    await asyncio.sleep(0)
    with pytest.raises(SessionSupervisorError, match="not available"):
        await collect(supervisor)
    await closing
    assert backend.messages == 0 and closed == [True]


@pytest.mark.asyncio
async def test_turn_deadline_stops_runtime_while_consumer_is_paused():
    stopped = asyncio.Event()

    async def close_runtime():
        stopped.set()

    supervisor = CopilotSessionSupervisor(
        pending_requests=frozenset, close_runtime=close_runtime, turn_timeout=0.03,
    )
    backend = Backend(supervisor)
    supervisor.bind(backend)
    stream = supervisor.stream("first")
    await anext(stream)
    # The deadline must stop the runtime without cancelling this consumer's
    # unrelated await or requiring another anext() to execute generator code.
    await asyncio.wait_for(stopped.wait(), 1)
    assert backend.disconnected
    with pytest.raises(SessionSupervisorError, match="turn exceeded its deadline"):
        await anext(stream)
    await supervisor.close()


@pytest.mark.asyncio
async def test_completed_turn_cancels_deadline_and_leaves_session_reusable():
    supervisor, backend, closed = make(turn_timeout=0.03)
    try:
        assert (await collect(supervisor))[-1].type == "done"
        await asyncio.sleep(0.06)
        assert not closed and not backend.disconnected
        assert (await collect(supervisor))[-1].type == "done"
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_explicit_close_cancels_paused_stream_deadline():
    supervisor, _, _ = make(turn_timeout=0.03)
    stream = supervisor.stream("first")
    await anext(stream)
    await supervisor.close()
    await asyncio.sleep(0.06)
    # Closing is terminal but must not turn into a later timeout failure.
    with pytest.raises(SessionSupervisorError, match="not available"):
        await anext(stream)


@pytest.mark.parametrize("limits", [
    {"turn_timeout": float("nan")}, {"turn_timeout": float("inf")}, {"turn_timeout": 0},
    {"rpc_timeout": float("nan")}, {"rpc_timeout": float("inf")}, {"rpc_timeout": "1"},
    {"queue_capacity": 1.5}, {"queue_capacity": True},
])
def test_session_limits_must_be_finite_and_positive(limits):
    with pytest.raises(ValueError, match="Positive Copilot session limits"):
        make(**limits)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["abort", "interrupt"])
async def test_accepted_control_blocks_steering_and_repeated_control_until_settled(control):
    supervisor, backend, _ = make()
    producer, _, owned = await hold_backend(supervisor, backend)
    snapshot_entered, release_snapshot = asyncio.Event(), asyncio.Event()

    async def pause_snapshot():
        snapshot_entered.set()
        await release_snapshot.wait()

    backend.snapshot_hook = pause_snapshot
    try:
        assert (await getattr(supervisor, control)()).accepted
        await asyncio.wait_for(snapshot_entered.wait(), 1)
        with pytest.raises(SessionSupervisorError, match="control awaiting settlement"):
            await supervisor.steer("would erase control proof")
        for repeated in (supervisor.abort, supervisor.interrupt):
            with pytest.raises(SessionSupervisorError, match="control awaiting settlement"):
                await repeated()
        assert backend.messages == 1 and not producer.done()
        release_snapshot.set()
        result = await asyncio.wait_for(producer, 1)
        assert result[-1].type == "done"
        assert any(event.type == "tool_result" and event.data["is_error"] for event in result)
        backend.snapshot_hook = None
        backend.send_hook = None
        assert (await collect(supervisor, "next independent turn"))[-1].type == "done"
        assert backend.messages == 2
    finally:
        release_snapshot.set()
        await supervisor.close()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_steer_waiting_for_control_ack_is_rejected_only_after_acceptance():
    supervisor, backend, _ = make()
    producer, _, owned = await hold_backend(supervisor, backend)
    ack_entered, release_ack, release_snapshot = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_abort = backend.abort

    async def delayed_abort():
        ack_entered.set()
        await release_ack.wait()
        await original_abort()

    async def delayed_snapshot():
        await release_snapshot.wait()

    backend.abort = delayed_abort
    backend.snapshot_hook = delayed_snapshot
    try:
        controlling = asyncio.create_task(supervisor.abort())
        await ack_entered.wait()
        steering = asyncio.create_task(supervisor.steer("queued behind acknowledgement"))
        await asyncio.sleep(0)
        assert not steering.done()
        release_ack.set()
        assert (await controlling).accepted
        with pytest.raises(SessionSupervisorError, match="control awaiting settlement"):
            await steering
        assert backend.messages == 1
        release_snapshot.set()
        assert (await producer)[-1].type == "done"
    finally:
        release_ack.set()
        release_snapshot.set()
        await supervisor.close()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_rejected_interrupt_does_not_block_normal_steering():
    supervisor, backend, _ = make()

    async def stay_processing(_message_id):
        backend.state = replace(EMPTY, processing=True)

    backend.send_hook = stay_processing
    backend.interrupt_accepted = False
    producer = asyncio.create_task(collect(supervisor))
    await backend.sent.wait()
    try:
        assert not (await supervisor.interrupt()).accepted
        backend.send_hook = None
        backend.state = EMPTY
        assert await supervisor.steer("continue after rejected interruption") == "message-2"
        assert (await asyncio.wait_for(producer, 1))[-1].type == "done"
        assert backend.messages == 2
    finally:
        await supervisor.close()

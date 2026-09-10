"""Native question completion is independent of a returned host callback."""

import asyncio
from dataclasses import replace

import pytest

from test_supervisor import EMPTY, SessionSupervisorError, collect, make


@pytest.mark.asyncio
async def test_host_question_answer_return_does_not_retire_native_request_or_finish_turn():
    supervisor, backend, _ = make()
    observed = asyncio.Event()

    async def answer():
        return {"answer": "yes", "wasFreeform": False}

    async def send_hook(_message_id):
        backend.emit("user_input.requested", {
            "requestId": "native-question", "question": "Continue?",
            "choices": ["yes", "no"], "allowFreeform": False, "toolCallId": "question-tool",
        })
        assert await supervisor.requests.run(answer) == {"answer": "yes", "wasFreeform": False}
        backend.emit("session.idle")

    async def snapshot_hook():
        observed.set()

    backend.send_hook = send_hook
    backend.snapshot_hook = snapshot_hook
    producer = asyncio.create_task(collect(supervisor))
    try:
        await asyncio.wait_for(observed.wait(), 1)
        assert not supervisor.requests.pending_ids
        snapshot = await supervisor._observe()
        assert snapshot.pending_messages == frozenset({"native-question"})
        assert not snapshot.is_settled() and not producer.done()
        backend.emit("user_input.completed", {
            "requestId": "native-question", "answer": "yes", "wasFreeform": False,
        })
        assert (await supervisor._observe()).pending_messages == frozenset()
        # Completion itself invalidates the preceding idle checkpoint.
        assert supervisor.coordinator.begin_reconciliation() is None
        backend.emit("session.idle")
        events = await asyncio.wait_for(producer, 1)
        assert sum(event.type == "done" for event in events) == 1
    finally:
        await supervisor.close()
        await asyncio.gather(producer, return_exceptions=True)


@pytest.mark.asyncio
async def test_request_and_completion_replays_cannot_resurrect_completed_question():
    supervisor, backend, _ = make()
    request = {"id": "request-event", "type": "user_input.requested", "data": {
        "requestId": "native-question", "question": "Continue?",
    }}
    completed = {"id": "complete-event", "type": "user_input.completed", "data": {
        "requestId": "native-question",
    }}
    try:
        supervisor.receive_event(request)
        supervisor.receive_event(completed)
        supervisor.receive_event(request)  # Same event ID and exact payload.
        supervisor.receive_event(completed)
        backend.emit("user_input.requested", request["data"])  # Distinct event ID, same native request.
        backend.emit("user_input.completed", completed["data"])
        assert (await supervisor._observe()).pending_messages == frozenset()
        assert (await collect(supervisor))[-1].type == "done"
    finally:
        await supervisor.close()


@pytest.mark.asyncio
async def test_completion_before_request_is_a_tombstone_and_question_text_is_not_identity():
    supervisor, backend, _ = make()
    try:
        backend.emit("user_input.completed", {"requestId": "already-finished"})
        backend.emit("user_input.requested", {"requestId": "already-finished", "question": "Same question"})
        for request_id in ("first", "second"):
            backend.emit("user_input.requested", {"requestId": request_id, "question": "Same question"})
        backend.emit("user_input.completed", {"requestId": "first"})
        assert (await supervisor._observe()).pending_messages == frozenset({"second"})
        backend.emit("user_input.completed", {"requestId": "second"})
        assert (await supervisor._observe()).pending_messages == frozenset()
    finally:
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user_input.requested", "user_input.completed"])
@pytest.mark.parametrize("request_id", [None, "", True, 7, {}, []])
async def test_malformed_native_question_identity_fails_closed_and_stops_runtime(kind, request_id):
    supervisor, backend, closed = make()
    backend.emit(kind, {"requestId": request_id, "question": "Do not leak this question"})
    with pytest.raises(SessionSupervisorError, match="event stream is invalid") as failure:
        await collect(supervisor)
    await supervisor.close()
    assert closed == [True] and backend.disconnected
    assert "Do not leak" not in str(failure.value)


@pytest.mark.asyncio
async def test_changed_payload_replay_cannot_retire_a_different_pending_question():
    supervisor, backend, closed = make()
    backend.emit("user_input.requested", {"requestId": "first", "question": "One"})
    backend.emit("user_input.requested", {"requestId": "second", "question": "Two"})
    completion = {"id": "finish-one", "type": "user_input.completed", "data": {"requestId": "first"}}
    supervisor.receive_event(completion)
    supervisor.receive_event({**completion, "data": {"requestId": "second"}})
    with pytest.raises(SessionSupervisorError, match="event stream is invalid"):
        await collect(supervisor)
    await supervisor.close()
    assert closed == [True] and backend.disconnected


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["external", "native_permissions", "native_messages"])
async def test_retired_native_question_does_not_turn_unknown_inventories_into_empty(unknown):
    supervisor, backend, _ = make(pending_requests=(lambda: None) if unknown == "external" else frozenset)
    if unknown == "native_permissions":
        backend.state = replace(EMPTY, pending_permissions=None)
    elif unknown == "native_messages":
        backend.state = replace(EMPTY, pending_messages=None)
    try:
        backend.emit("user_input.requested", {"requestId": "question", "question": "Continue?"})
        backend.emit("user_input.completed", {"requestId": "question"})
        snapshot = await supervisor._observe()
        assert not snapshot.is_settled()
        if unknown == "native_messages":
            assert snapshot.pending_messages is None
        else:
            assert snapshot.pending_permissions is None
    finally:
        await supervisor.close()

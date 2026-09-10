"""Native cancelled-permission proof is distinct from joined host callbacks."""

import asyncio
from dataclasses import replace
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.coordinator import (  # noqa: E402
    CopilotTurnCoordinator, SettlementObservation,
)
from core.layers.copilot.requests import RequestUnavailableError  # noqa: E402
from core.layers.copilot.supervisor import (  # noqa: E402
    CopilotSessionSupervisor, RuntimeSnapshot,
)


EMPTY = RuntimeSnapshot(False, (), frozenset(), frozenset())


class Backend:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.serial = 0
        self.state = EMPTY
        self.outcome = "cancelled"
        self.completed_tool = "native-tool"
        self.aborted = True
        self.barriers = 0

    def emit(self, kind, data=None):
        self.serial += 1
        self.supervisor.receive_event({"id": str(self.serial), "type": kind, "data": data or {}})

    async def send(self, _prompt, *, immediate=False):
        self.emit("user.message", {"messageId": "input"})
        self.emit("assistant.turn_start", {"turnId": "turn"})
        self.emit("tool.execution_start", {"toolCallId": "native-tool", "toolName": "fixture"})
        self.emit("permission.requested", {
            "requestId": "permission-1",
            "permissionRequest": {"kind": "custom-tool", "toolCallId": "native-tool"},
        })
        return "input"

    def permission_completed(self):
        if self.outcome is not None:
            self.emit("permission.completed", {
                "requestId": "permission-1", "toolCallId": self.completed_tool,
                "result": {"kind": self.outcome},
            })

    async def abort(self):
        self.permission_completed()
        self.emit("session.idle", {"aborted": self.aborted})

    async def interrupt(self):
        self.permission_completed()
        self.emit("assistant.idle")
        return True

    async def snapshot(self):
        return self.state

    async def is_processing(self):
        self.barriers += 1
        return False

    async def disconnect(self):
        pass


async def start(*, external=frozenset):
    async def close_runtime():
        pass

    supervisor = CopilotSessionSupervisor(
        pending_requests=external, close_runtime=close_runtime, rpc_timeout=0.02, turn_timeout=2,
    )
    backend = Backend(supervisor)
    supervisor.bind(backend)
    stream = supervisor.stream("hold at approval")
    while (await anext(stream)).type != "tool_use":
        pass
    return supervisor, backend, stream


def synthesized(supervisor):
    return [event for event in supervisor._events if event.type in {"tool_result", "done"}]


def assert_permission_error_and_done(events):
    assert [event.type for event in events] == ["tool_result", "done"]
    assert events[0].data["tool_id"] == "native-tool"
    assert events[0].data["is_error"] is True
    assert "permission request was cancelled" in str(events[0].data)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["abort", "interrupt"])
async def test_accepted_control_settles_missing_native_result_only_after_host_request_drains(control):
    supervisor, backend, stream = await start()
    started, cancelled, release = (asyncio.Event() for _ in range(3))

    async def resistant_policy():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return "late approval"

    waiter = asyncio.create_task(supervisor.requests.run(resistant_policy))
    await started.wait()
    try:
        ack = await getattr(supervisor, control)()
        assert ack.accepted and not ack.callbacks_stopped and cancelled.is_set()
        await supervisor._reconcile()
        assert synthesized(supervisor) == []
        release.set()
        with pytest.raises(RequestUnavailableError):
            await waiter
        await supervisor._reconcile()
        assert_permission_error_and_done(synthesized(supervisor))
        if control == "interrupt":
            assert backend.barriers >= 1
        outputs = [event async for event in stream]
        assert len([event for event in outputs if event.type == "done"]) == 1
        assert len([event for event in outputs if event.type == "tool_result"]) == 1
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)
        await stream.aclose()
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, "denied", "approved", "unknown-new-outcome"])
async def test_abort_ack_without_explicit_native_cancelled_outcome_does_not_settle(outcome):
    supervisor, backend, stream = await start()
    backend.outcome = outcome
    try:
        assert (await supervisor.abort()).accepted
        await supervisor._reconcile()
        assert synthesized(supervisor) == []
    finally:
        await stream.aclose()
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted_abort", [False, True])
async def test_native_cancelled_proof_with_normal_idle_does_not_settle(accepted_abort):
    supervisor, backend, stream = await start()
    try:
        if accepted_abort:
            backend.aborted = False
            await supervisor.abort()
        else:
            backend.permission_completed()
            backend.emit("session.idle")
        await supervisor._reconcile()
        assert synthesized(supervisor) == []
    finally:
        await stream.aclose()
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", ["tool", "native_request", "external_inventory", "native_inventory"])
async def test_uncorrelated_tool_or_unresolved_requests_block_cancelled_permission_settlement(unknown):
    supervisor, backend, stream = await start(external=(lambda: None) if unknown == "external_inventory" else frozenset)
    if unknown == "tool":
        backend.completed_tool = "different-tool"
    elif unknown == "native_request":
        backend.state = replace(EMPTY, pending_permissions=frozenset({"another-permission"}))
    elif unknown == "native_inventory":
        backend.state = replace(EMPTY, pending_permissions=None)
    try:
        if unknown == "tool":
            await supervisor.abort()  # RPC ACK may arrive even though event correlation poisoned the session.
            assert supervisor._failure is not None
        else:
            await supervisor.abort()
            await supervisor._reconcile()
        assert synthesized(supervisor) == []
    finally:
        await stream.aclose()
        await supervisor.close()


@pytest.mark.asyncio
async def test_queued_and_completed_done_pause_requests_until_next_stream():
    supervisor, backend, stream = await start()

    async def forbidden():
        pytest.fail("policy request was admitted after the turn settled")

    try:
        await supervisor.abort()
        await supervisor._reconcile()
        assert_permission_error_and_done(synthesized(supervisor))
        with pytest.raises(RequestUnavailableError):
            await supervisor.requests.run(forbidden)
        remaining = [event async for event in stream]
        assert remaining[-1].type == "done"
        with pytest.raises(RequestUnavailableError):
            await supervisor.requests.run(forbidden)

        async def fresh_policy():
            return "fresh turn allowed"

        async def next_send(_prompt, *, immediate=False):
            assert await supervisor.requests.run(fresh_policy) == "fresh turn allowed"
            backend.emit("user.message", {"messageId": "second-input"})
            backend.emit("assistant.turn_start", {"turnId": "second-turn"})
            backend.emit("session.idle")
            return "second-input"

        backend.send = next_send
        outputs = [event async for event in supervisor.stream("fresh turn")]
        assert [event.type for event in outputs].count("done") == 1
    finally:
        await stream.aclose()
        await supervisor.close()


class CoordinatorFixture:
    def __init__(self):
        self.coordinator = CopilotTurnCoordinator()
        self.serial = 0
        self.emit("assistant.turn_start", {"turnId": "turn"})
        self.emit("tool.execution_start", {"toolCallId": "native-tool", "toolName": "fixture"})

    def emit(self, kind, data=None):
        self.serial += 1
        return self.coordinator.receive_event(self.serial, {
            "id": str(self.serial), "type": kind, "data": data or {},
        })

    def abort(self):
        ticket = self.coordinator.request_abort()
        self.coordinator.acknowledge_abort(ticket, accepted=True)
        self.emit("session.idle", {"aborted": True})
        return self.coordinator.begin_reconciliation()


def observation(**changes):
    return replace(SettlementObservation(
        False, (), frozenset(), frozenset(), pending_messages=frozenset(),
        cancelled_permission_tool_ids=frozenset({"native-tool"}),
    ), **changes)


def test_coordinator_native_permission_proof_emits_distinct_error_and_suppresses_late_completion():
    fixture = CoordinatorFixture()
    checkpoint = fixture.abort()
    assert_permission_error_and_done(fixture.coordinator.finish_reconciliation(checkpoint, observation()))
    assert fixture.emit("tool.execution_complete", {"toolCallId": "native-tool", "success": False}) == []
    fixture.emit("session.idle", {"aborted": True})
    checkpoint = fixture.coordinator.begin_reconciliation()
    assert fixture.coordinator.finish_reconciliation(checkpoint, observation(
        cancelled_permission_tool_ids=frozenset(),
    )) == []


def test_coordinator_unknown_permission_proof_never_clears_unrelated_open_tool():
    fixture = CoordinatorFixture()
    checkpoint = fixture.abort()
    with pytest.raises(ValueError):
        fixture.coordinator.finish_reconciliation(checkpoint, observation(
            cancelled_permission_tool_ids=frozenset({"native-tool", "unknown-tool"}),
        ))
    assert_permission_error_and_done(fixture.coordinator.finish_reconciliation(checkpoint, observation()))


def test_unacknowledged_abort_cannot_authorize_synthetic_permission_tool_result():
    fixture = CoordinatorFixture()
    ticket = fixture.coordinator.request_abort()
    fixture.emit("session.idle", {"aborted": True})
    checkpoint = fixture.coordinator.begin_reconciliation()
    assert fixture.coordinator.finish_reconciliation(checkpoint, observation()) == []
    assert fixture.coordinator.acknowledge_abort(ticket, accepted=True)
    checkpoint = fixture.coordinator.begin_reconciliation()
    assert_permission_error_and_done(fixture.coordinator.finish_reconciliation(checkpoint, observation()))


@pytest.mark.parametrize("blocked", [
    {"pending_tools": frozenset({"host-callback"})},
    {"pending_permissions": frozenset({"host-prompt"})},
    {"pending_messages": frozenset({"native-question"})},
    {"processing": None},
])
def test_coordinator_permission_proof_never_bypasses_other_settlement_barriers(blocked):
    fixture = CoordinatorFixture()
    checkpoint = fixture.abort()
    assert fixture.coordinator.finish_reconciliation(checkpoint, observation(**blocked)) == []

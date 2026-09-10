"""Native shell ownership with a controlled SDK-free transport."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "proxy"))
from core.layers.copilot.coordinator import SettlementObservation, TaskState  # noqa: E402
from core.layers.copilot.native_shells import CopilotNativeShellSession, NativeShellError  # noqa: E402
from core.layers.copilot.supervisor import CopilotSessionSupervisor  # noqa: E402
from core.layers.copilot.requests import RequestUnavailableError  # noqa: E402

START = datetime(2026, 9, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def rpc_types(monkeypatch):
    rpc = ModuleType("copilot.rpc")
    rpc.TasksCancelRequest = NS
    rpc.InterruptMainTurnRequest = NS
    monkeypatch.setitem(sys.modules, "copilot.rpc", rpc)


def shell(identity="shell-one", status="running", **changes):
    return NS(id=identity, status=status, type="shell", attachment_mode="attached",
              command="sleep 1", started_at=START, execution_mode="background", **changes)


def changed(task, **changes):
    return NS(**{**vars(task), **changes})


class Session:
    def __init__(self, tasks=None):
        self.session_id = "owned-session"
        self.tasks = [] if tasks is None else tasks
        self.calls = []
        self.rpc = NS(
            tasks=NS(refresh=AsyncMock(side_effect=self.refresh), list=AsyncMock(side_effect=self.list_tasks),
                     cancel=AsyncMock(return_value=NS(cancelled=True))),
            permissions=NS(pending_requests=AsyncMock(return_value=NS(items=[]))),
            queue=NS(pending_items=AsyncMock(return_value=NS(items=[], steering_messages=[]))),
            metadata=NS(is_processing=AsyncMock(return_value=NS(processing=False))),
            interrupt_main_turn=AsyncMock(return_value=NS(interrupted=True)),
        )
        self.abort = AsyncMock()
        self.send = AsyncMock(return_value="message-one")
        self.disconnect = AsyncMock()

    async def refresh(self, **_kwargs):
        self.calls.append("refresh")

    async def list_tasks(self, **_kwargs):
        self.calls.append("list")
        return NS(tasks=self.tasks)


def owned(sdk, **options):
    return CopilotNativeShellSession(
        sdk, processes_settled=options.pop("processes_settled", lambda: True), **options,
    )


def settled(snapshot):
    return SettlementObservation(snapshot.processing, snapshot.tasks, snapshot.pending_permissions,
                                 frozenset(), pending_messages=snapshot.pending_messages).is_settled()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [
    ("running", TaskState.RUNNING), ("idle", TaskState.IDLE), ("orphaned", TaskState.ORPHANED),
    ("future-state", TaskState.UNKNOWN), ("completed", TaskState.COMPLETED),
    ("failed", TaskState.FAILED), ("cancelled", TaskState.CANCELLED),
])
async def test_all_shell_states_are_retained_and_only_terminal_states_settle(status, expected):
    sdk = Session([shell(status=status)])
    snapshot = await owned(sdk).snapshot()
    assert snapshot.tasks[0].state is expected
    assert settled(snapshot) is (status in {"completed", "failed", "cancelled"})
    assert sdk.calls == ["refresh", "list"]


@pytest.mark.asyncio
async def test_missing_running_shell_remains_unknown_until_fresh_terminal_state():
    sdk = Session([shell()])
    proof = [False]
    adapter = owned(sdk, processes_settled=lambda: proof[0])
    await adapter.snapshot()
    sdk.tasks = []
    missing = await adapter.snapshot()
    assert missing.tasks[0].state is TaskState.UNKNOWN
    assert not settled(missing)
    sdk.tasks = [shell(status="completed")]
    proof[0] = True
    assert settled(await adapter.snapshot())
    sdk.tasks = []
    tombstone = await adapter.snapshot()
    assert tombstone.tasks[0].state is TaskState.COMPLETED
    assert settled(tombstone)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [
    changed(shell(), command="different private command"),
    changed(shell(), started_at=START + timedelta(seconds=1)),
    shell(status="running"), shell(status="failed"),
])
async def test_terminal_tombstone_rejects_identity_reuse_and_status_regression(replacement):
    sdk = Session([shell(status="completed")])
    adapter = owned(sdk)
    await adapter.snapshot()
    sdk.tasks = []
    await adapter.snapshot()
    sdk.tasks = [replacement]
    with pytest.raises(NativeShellError) as error:
        await adapter.snapshot()
    assert "private" not in str(error.value) and error.value.__context__ is None
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")
    sdk.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"type": "agent"}, {"type": "client"}, {"type": "unknown"},
    {"attachment_mode": "detached"}, {"attachment_mode": None},
    {"execution_mode": None}, {"execution_mode": "future"},
    {"command": ""}, {"command": None}, {"started_at": None}, {"started_at": "2026-09-10"},
])
async def test_unsupported_shell_profile_poisoned_instead_of_ignored(changes):
    sdk = Session([changed(shell(), **changes)])
    adapter = owned(sdk)
    with pytest.raises(NativeShellError):
        await adapter.snapshot()
    sdk.tasks = []
    with pytest.raises(NativeShellError):
        await adapter.snapshot()
    assert sdk.rpc.tasks.list.await_count == 1


@pytest.mark.asyncio
async def test_sync_to_background_transition_is_allowed_without_rebinding_identity():
    sdk = Session([changed(shell(), execution_mode="sync")])
    adapter = owned(sdk)
    assert not settled(await adapter.snapshot())
    sdk.tasks = [shell()]
    assert not settled(await adapter.snapshot())


@pytest.mark.asyncio
async def test_tombstone_count_is_bounded_across_turns_and_metadata_is_not_retained():
    private = "private shell command should not persist"
    sdk = Session([changed(shell(status="completed"), command=private)])
    adapter = owned(sdk, maximum_shells=1)
    first = await adapter.snapshot()
    assert private not in repr(first) and private not in repr(adapter._shells)
    sdk.tasks = [shell(identity="second", status="completed")]
    with pytest.raises(NativeShellError):
        await adapter.snapshot()


@pytest.mark.asyncio
async def test_session_identity_change_during_rpc_poisoned_before_return():
    sdk = Session()
    adapter = owned(sdk)

    async def refresh(**_kwargs):
        sdk.session_id = "different-session"

    sdk.rpc.tasks.refresh.side_effect = refresh
    with pytest.raises(NativeShellError):
        await adapter.snapshot()
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")
    sdk.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
@pytest.mark.parametrize("control", ["abort", "interrupt"])
async def test_cancel_ack_never_joins_shell_before_fresh_terminal_inventory(accepted, control):
    sdk = Session([shell()])
    adapter = owned(sdk, cancel_timeout=1)
    ack = asyncio.Event()
    terminal = asyncio.Event()

    async def cancel(request, **_kwargs):
        assert request.id == "shell-one"
        ack.set()
        return NS(cancelled=accepted)

    async def refresh(**_kwargs):
        if ack.is_set():
            await terminal.wait()
            sdk.tasks = [shell(status="cancelled" if accepted else "completed")]

    sdk.rpc.tasks.cancel.side_effect = cancel
    sdk.rpc.tasks.refresh.side_effect = refresh
    pending = asyncio.create_task(getattr(adapter, control)())
    await asyncio.wait_for(ack.wait(), 1)
    assert not pending.done()
    terminal.set()
    result = await asyncio.wait_for(pending, 1)
    assert result is (True if control == "interrupt" else None)
    assert sdk.rpc.tasks.cancel.await_count == 1
    assert settled(await adapter.snapshot())


@pytest.mark.asyncio
async def test_rejected_interrupt_preserves_shell_and_never_requests_cancellation():
    sdk = Session([shell()])
    sdk.rpc.interrupt_main_turn.return_value = NS(interrupted=False)
    adapter = owned(sdk)
    assert await adapter.interrupt() is False
    sdk.rpc.tasks.cancel.assert_not_awaited()
    assert not settled(await adapter.snapshot())
    assert await adapter.send("still active") == "message-one"


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [True, False])
async def test_disappearance_after_cancel_ack_stays_unknown_and_hits_deadline(ack):
    sdk = Session([shell()])
    adapter = owned(sdk, processes_settled=lambda: False, cancel_timeout=0.12)

    async def cancel(_request, **_kwargs):
        sdk.tasks = []
        return NS(cancelled=ack)

    sdk.rpc.tasks.cancel.side_effect = cancel
    with pytest.raises(NativeShellError):
        await asyncio.wait_for(adapter.abort(), 1)
    assert sdk.rpc.tasks.cancel.await_count == 1
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")
    sdk.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [None, 0, 1, "true"])
async def test_malformed_cancellation_ack_poisoned(ack):
    sdk = Session([shell()])
    sdk.rpc.tasks.cancel.return_value = NS(cancelled=ack)
    adapter = owned(sdk)
    with pytest.raises(NativeShellError):
        await adapter.abort()
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["refresh", "list", "cancel"])
async def test_inventory_and_cancellation_errors_are_sanitized_and_sticky(stage):
    sdk = Session([shell()])
    getattr(sdk.rpc.tasks, stage).side_effect = RuntimeError("secret transport payload")
    adapter = owned(sdk)
    with pytest.raises(NativeShellError) as error:
        await adapter.abort()
    assert "secret" not in str(error.value) and error.value.__context__ is None
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["abort", "interrupt"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_uncertain_native_control_poisoned_and_error_sanitized(control, cancelled):
    sdk = Session([shell()])
    rpc = sdk.abort if control == "abort" else sdk.rpc.interrupt_main_turn
    rpc.side_effect = asyncio.CancelledError() if cancelled else RuntimeError("secret native control payload")
    adapter = owned(sdk)
    with pytest.raises(asyncio.CancelledError if cancelled else NativeShellError) as error:
        await getattr(adapter, control)()
    assert "secret" not in str(error.value)
    if not cancelled:
        assert error.value.__context__ is None
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")
    sdk.send.assert_not_awaited()
    sdk.rpc.tasks.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_deadline_cancels_stalled_refresh_and_poisoned():
    sdk = Session()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def refresh(**_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    sdk.rpc.tasks.refresh.side_effect = refresh
    adapter = owned(sdk, rpc_timeout=0.02)
    with pytest.raises(NativeShellError):
        await asyncio.wait_for(adapter.snapshot(), 1)
    assert entered.is_set() and cancelled.is_set()
    sdk.rpc.tasks.list.assert_not_awaited()
    with pytest.raises(NativeShellError):
        await adapter.snapshot()


@pytest.mark.parametrize("options", [
    {"maximum_shells": 0}, {"maximum_shells": True}, {"maximum_shells": 1.5},
    {"rpc_timeout": 0}, {"rpc_timeout": True}, {"rpc_timeout": float("nan")},
    {"cancel_timeout": -1}, {"cancel_timeout": float("inf")},
])
def test_shell_owner_requires_finite_positive_bounds(options):
    with pytest.raises(ValueError):
        owned(Session(), **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["abort", "interrupt"])
async def test_control_rpc_deadline_poisoned_without_claiming_shell_cancel(control):
    sdk = Session([shell()])
    stopped = asyncio.Event()

    async def stalled(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    rpc = sdk.abort if control == "abort" else sdk.rpc.interrupt_main_turn
    rpc.side_effect = stalled
    adapter = owned(sdk, rpc_timeout=0.02)
    with pytest.raises(NativeShellError):
        await asyncio.wait_for(getattr(adapter, control)(), 1)
    assert stopped.is_set()
    sdk.rpc.tasks.cancel.assert_not_awaited()
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")


@pytest.mark.asyncio
async def test_cancel_waiter_cancellation_poisoned_without_forgetting_shell():
    sdk = Session([shell()])
    entered = asyncio.Event()

    async def cancel(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    sdk.rpc.tasks.cancel.side_effect = cancel
    adapter = owned(sdk)
    control = asyncio.create_task(adapter.abort())
    await asyncio.wait_for(entered.wait(), 1)
    control.cancel()
    with pytest.raises(asyncio.CancelledError):
        await control
    assert "shell-one" in adapter._shells
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")


@pytest.mark.asyncio
async def test_supervisor_pauses_policy_before_native_control_and_waits_for_shell_join():
    sdk = Session([shell()])
    runtime_closed = AsyncMock()
    supervisor = CopilotSessionSupervisor(
        pending_requests=frozenset, close_runtime=runtime_closed, rpc_timeout=2, turn_timeout=3,
    )
    supervisor.bind(owned(sdk, cancel_timeout=1))
    started = asyncio.Event()
    cancel_ack = asyncio.Event()
    terminal = asyncio.Event()
    serial = 0
    forbidden = AsyncMock(return_value={"decision": "allow"})

    def emit(kind, data):
        nonlocal serial
        serial += 1
        supervisor.receive_event({"id": f"event-{serial}", "type": kind, "data": data})

    async def send(prompt, **_kwargs):
        emit("user.message", {"messageId": "message-one", "content": prompt})
        emit("assistant.turn_start", {"turnId": "turn-one"})
        emit("assistant.message", {"messageId": "reply-one", "content": "Shell is running"})
        started.set()
        return "message-one"

    async def abort():
        with pytest.raises(RequestUnavailableError):
            await supervisor.requests.run(forbidden)
        emit("session.idle", {"aborted": True})

    async def cancel(_request, **_kwargs):
        with pytest.raises(RequestUnavailableError):
            await supervisor.requests.run(forbidden)
        cancel_ack.set()
        return NS(cancelled=True)

    async def refresh(**_kwargs):
        if cancel_ack.is_set():
            await terminal.wait()
            sdk.tasks = [shell(status="cancelled")]

    sdk.send.side_effect = send
    sdk.abort.side_effect = abort
    sdk.rpc.tasks.cancel.side_effect = cancel
    sdk.rpc.tasks.refresh.side_effect = refresh
    stream = supervisor.stream("fixture")
    control = None
    try:
        first = await asyncio.wait_for(anext(stream), 1)
        assert first.type != "done" and started.is_set()
        # The consumer stays paused while control/ownership work proceeds.
        control = asyncio.create_task(supervisor.abort())
        await asyncio.wait_for(cancel_ack.wait(), 1)
        assert not control.done()
        forbidden.assert_not_awaited()
        terminal.set()
        assert (await asyncio.wait_for(control, 1)).accepted is True
        rest = [event async for event in stream]
        assert sum(event.type == "done" for event in rest) == 1
    finally:
        terminal.set()
        if control and not control.done():
            control.cancel()
            await asyncio.gather(control, return_exceptions=True)
        await stream.aclose()
        await supervisor.close()
    runtime_closed.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("process_state", [False, None])
async def test_terminal_native_task_does_not_settle_without_process_proof(process_state):
    sdk = Session([shell(status="completed")])
    adapter = owned(sdk, processes_settled=lambda: process_state)
    snapshot = await adapter.snapshot()
    assert not settled(snapshot)
    assert any(task.state not in {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
               for task in snapshot.tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("process_state", [False, None])
async def test_empty_native_tasks_do_not_settle_without_process_proof(process_state):
    snapshot = await owned(Session(), processes_settled=lambda: process_state).snapshot()
    assert not settled(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("process_state", [0, 1, "true", [], {}])
async def test_invalid_process_proof_is_rejected_and_poisoned(process_state):
    sdk = Session()
    adapter = owned(sdk, processes_settled=lambda: process_state)
    with pytest.raises(NativeShellError):
        await adapter.snapshot()
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")
    sdk.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_proof_failure_is_sanitized_and_poisoned():
    def unavailable():
        raise RuntimeError("private process command")

    sdk = Session()
    adapter = owned(sdk, processes_settled=unavailable)
    with pytest.raises(NativeShellError) as error:
        await adapter.snapshot()
    assert "private" not in str(error.value) and error.value.__context__ is None
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")


@pytest.mark.asyncio
async def test_process_proof_is_checked_after_native_rpc_observations():
    proof = [True]
    sdk = Session([shell(status="completed")])

    async def refresh(**_kwargs):
        proof[0] = False

    sdk.rpc.tasks.refresh.side_effect = refresh
    adapter = owned(sdk, processes_settled=lambda: proof[0])
    assert not settled(await adapter.snapshot())
    sdk.rpc.tasks.refresh.side_effect = None
    proof[0] = True
    assert settled(await adapter.snapshot())


@pytest.mark.asyncio
async def test_native_cancel_completion_still_waits_for_process_fence_without_cancelling_synthetic_id():
    sdk = Session([shell()])
    process_alive = [True]
    fence_read = asyncio.Event()

    def processes_settled():
        if sdk.tasks[0].status == "completed":
            fence_read.set()
        return not process_alive[0]

    async def cancel(request, **_kwargs):
        assert request.id == "shell-one"
        sdk.tasks = [shell(status="completed")]
        return NS(cancelled=True)

    sdk.rpc.tasks.cancel.side_effect = cancel
    adapter = owned(sdk, processes_settled=processes_settled, cancel_timeout=1)
    control = asyncio.create_task(adapter.abort())
    try:
        await asyncio.wait_for(fence_read.wait(), 1)
        assert not control.done()
        assert sdk.rpc.tasks.cancel.await_count == 1
        process_alive[0] = False
        await asyncio.wait_for(control, 1)
        assert settled(await adapter.snapshot())
        assert sdk.rpc.tasks.cancel.await_count == 1
    finally:
        if not control.done():
            control.cancel()
            await asyncio.gather(control, return_exceptions=True)


@pytest.mark.asyncio
async def test_processes_without_native_tasks_hit_control_deadline_without_fake_cancel_rpc():
    sdk = Session()
    adapter = owned(sdk, processes_settled=lambda: False, cancel_timeout=0.02)
    with pytest.raises(NativeShellError):
        await adapter.abort()
    sdk.rpc.tasks.cancel.assert_not_awaited()
    with pytest.raises(NativeShellError):
        await adapter.send("must not dispatch")


def test_process_owner_is_required_explicitly():
    with pytest.raises(TypeError):
        CopilotNativeShellSession(Session())


@pytest.mark.asyncio
async def test_absent_owned_shell_retires_only_after_process_fence_proves_exit():
    sdk = Session([shell()])
    proof = [False]
    adapter = owned(sdk, processes_settled=lambda: proof[0])
    assert not settled(await adapter.snapshot())
    sdk.tasks = []
    assert not settled(await adapter.snapshot())
    proof[0] = True
    snapshot = await adapter.snapshot()
    assert snapshot.tasks[0].state is TaskState.RETIRED
    assert settled(snapshot)
    assert (await adapter.snapshot()).tasks[0].state is TaskState.RETIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "completed", "cancelled", "retired"])
async def test_reappearing_retired_shell_id_is_never_readmitted(status):
    sdk = Session([shell()])
    adapter = owned(sdk)
    await adapter.snapshot()
    sdk.tasks = []
    assert (await adapter.snapshot()).tasks[0].state is TaskState.RETIRED
    sdk.tasks = [shell(status=status)]
    with pytest.raises(NativeShellError):
        await adapter.snapshot()


@pytest.mark.asyncio
async def test_native_retired_status_is_not_host_retirement_proof():
    sdk = Session([shell(status="retired")])
    snapshot = await owned(sdk).snapshot()
    assert snapshot.tasks[0].state is TaskState.UNKNOWN
    assert not settled(snapshot)
